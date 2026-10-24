import asyncio
import subprocess

import asyncssh
import pytest

from app.agent.context import ContextInput, WorkspaceView, build_messages, system_prompt
from app.tables import Message
from app.workspaces.base import WorkspaceError, confine
from tests.conftest import chat, free_port, models_by_name

# ---------------------------------------------------------------- SSH


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def public_key_auth_supported(self) -> bool:
        return True


async def _exec(process: asyncssh.SSHServerProcess) -> None:
    proc = await asyncio.create_subprocess_shell(
        process.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    process.stdout.write(out.decode())
    process.exit(proc.returncode or 0)


@pytest.fixture(scope="module")
async def ssh_server(tmp_path_factory):
    d = tmp_path_factory.mktemp("ssh")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    (d / "authorized").write_text(client_key.export_public_key().decode())
    port = free_port()
    server = await asyncssh.create_server(
        _Server,
        "127.0.0.1",
        port,
        server_host_keys=[host_key],
        authorized_client_keys=str(d / "authorized"),
        sftp_factory=True,
        process_factory=_exec,
    )
    yield port, client_key.export_private_key().decode(), host_key
    server.close()


async def test_ssh_workspace_end_to_end(client, ssh_server, tmp_path):
    port, private_key, host_key = ssh_server
    root = tmp_path / "remote"
    root.mkdir()
    (root / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    conn = (
        await client.post(
            "/api/ssh",
            json={"name": f"local-{port}", "host": "127.0.0.1", "port": port, "username": "dev", "private_key": private_key},
        )
    ).json()
    assert conn["host_key_fingerprint"] is None
    assert not (await client.post(f"/api/ssh/{conn['id']}/test")).json()["ok"]  # not trusted yet

    scan = (await client.post(f"/api/ssh/{conn['id']}/scan-host-key")).json()
    assert scan["fingerprint"] == host_key.get_fingerprint()
    bad = await client.post(f"/api/ssh/{conn['id']}/trust-host-key", json={"fingerprint": "SHA256:wrong"})
    assert bad.status_code == 409
    trusted = (await client.post(f"/api/ssh/{conn['id']}/trust-host-key", json={"fingerprint": scan["fingerprint"]})).json()
    assert trusted["host_key_fingerprint"] == scan["fingerprint"]
    assert (await client.post(f"/api/ssh/{conn['id']}/test")).json() == {"ok": True, "detail": "ok"}
    listing = (await client.get(f"/api/ssh/{conn['id']}/browse", params={"path": str(tmp_path)})).json()
    assert "remote" in [e["name"] for e in listing["entries"]]

    models = await models_by_name(client)
    alpha = models["mock-alpha"]["id"]
    s = (
        await client.post(
            "/api/sessions",
            json={"title": "ssh", "model_ids": [alpha], "workspace": {"kind": "ssh", "ssh_connection_id": conn["id"], "path": str(root)}},
        )
    ).json()
    assert s["workspace_label"].startswith("ssh:dev@127.0.0.1:")
    await client.post(f"/api/sessions/{s['id']}/pick-builder", json={"model_id": alpha})
    await client.patch(f"/api/sessions/{s['id']}", json={"approve_commands": "auto"})
    prompt = "\n".join(
        [
            "go",
            '@tool write_file {"path": "pkg/mod.py", "content": "y = 2\\n"}',
            '@tool grep {"pattern": "x = ", "glob": "*.py"}',
            '@tool run_command {"command": "ls pkg && echo remote-ok"}',
            '@tool list_dir {"path": "/"}',
        ]
    )
    events = await chat(client, s["id"], prompt)
    results = [e["message"]["content"] for e in events if e["type"] == "tool_result"]
    assert results[0].startswith("Created pkg/mod.py")
    assert "app.py:1:x = 1" in results[1]
    assert "mod.py" in results[2] and "remote-ok" in results[2] and "[exit code 0]" in results[2]
    assert "outside the workspace" in results[3]
    assert (root / "pkg/mod.py").read_text() == "y = 2\n"

    # git snapshots over SSH (used for agent CLI turns)
    from app import db as dbmod
    from app.tables import SshConnection
    from app.workspaces.ssh import SshWorkspace

    async with dbmod.sessionmaker()() as db:
        row = await db.get(SshConnection, conn["id"])
    ws = SshWorkspace(row, str(root))
    ref = await ws.snapshot_begin()
    assert ref
    (root / "app.py").write_text("x = 3\n")
    (root / "pkg/mod.py").unlink()
    changes = {c.path: c for c in await ws.snapshot_changes(ref)}
    assert changes["app.py"].before == "x = 1\n" and changes["app.py"].after == "x = 3\n"
    assert changes["pkg/mod.py"].after is None

    # a changed host key is refused
    async with dbmod.sessionmaker()() as db:
        row = await db.get(SshConnection, conn["id"])
        row.host_key = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()
        await db.commit()
    r = (await client.post(f"/api/ssh/{conn['id']}/test")).json()
    assert not r["ok"] and "does not match" in r["detail"]


def test_confine():
    assert confine("/srv/app", "a/b.py") == "/srv/app/a/b.py"
    assert confine("/srv/app", ".") == "/srv/app"
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(WorkspaceError):
            confine("/srv/app", bad)
    assert confine("C:\\src\\app", "lib\\x.py", windows=True) == "C:\\src\\app\\lib\\x.py"
    with pytest.raises(WorkspaceError):
        confine("C:\\src\\app", "..\\..\\Windows", windows=True)


# ---------------------------------------------------------------- web


async def test_web_fetch_tool(client, server):
    from app.web.fetch import FetchError, web_fetch

    text = await web_fetch(f"{server}/api/health", allow_private=True)
    assert '"ok":true' in text.replace(" ", "")
    with pytest.raises(FetchError):
        await web_fetch(f"{server}/api/health", allow_private=False)
    with pytest.raises(FetchError):
        await web_fetch("file:///etc/passwd", allow_private=True)

    models = await models_by_name(client)
    s = (await client.post("/api/sessions", json={"title": "web", "model_ids": [models["mock-gamma"]["id"]]})).json()
    events = await chat(client, s["id"], f'read this\n@tool web_fetch {{"url": "{server}/api/health"}}')
    result = next(e["message"]["content"] for e in events if e["type"] == "tool_result")
    assert "Status: 200" in result
    # web_search is only offered when configured
    events = await chat(client, s["id"], '@tool web_search {"query": "x"}')
    assert not [e for e in events if e["type"] == "tool_result"]


# ---------------------------------------------------------------- context


def _msg(i, role, model_id=None, content="", recipients=None, phase="plan", tool_calls=None, tool_call_id=None):
    return Message(
        id=i, seq=i, session_id=1, turn_id="t", role=role, model_id=model_id, content=content,
        recipients=recipients, phase=phase, tool_calls=tool_calls, tool_call_id=tool_call_id,
    )


def _ci(model_id, mode, messages, phase="plan", votes=None, ws=None):
    return ContextInput(
        model_id=model_id, model_name={1: "A", 2: "B"}[model_id], phase=phase, context_mode=mode, messages=messages,
        names={1: "A", 2: "B"}, votes=votes or {}, workspace=ws or WorkspaceView(), has_tools=ws is not None,
    )


HISTORY = [
    _msg(1, "user", content="q1", recipients=[1, 2]),
    _msg(2, "assistant", 1, "A answer"),
    _msg(3, "assistant", 2, "B answer"),
    _msg(4, "user", content="q2 for B", recipients=[2]),
    _msg(5, "assistant", 2, "B again"),
]


def test_isolated_context_hides_other_models():
    msgs = build_messages(_ci(1, "isolated", HISTORY))
    assert [(m.role, m.content) for m in msgs] == [("user", "q1"), ("assistant", "A answer")]


def test_shared_context_shows_everyone():
    msgs = build_messages(_ci(1, "shared", HISTORY))
    assert msgs[0].content == "q1"
    assert msgs[1].role == "assistant"
    assert msgs[2].role == "user"
    assert "[B said]: B answer" in msgs[2].content
    assert "[User, to B]: q2 for B" in msgs[2].content and "[B said]: B again" in msgs[2].content


def test_tool_calls_keep_results_and_patch_missing_ones():
    history = [
        _msg(1, "user", content="do", recipients=[1]),
        _msg(2, "assistant", 1, "", tool_calls=[{"id": "c1", "name": "list_dir", "arguments": {}}, {"id": "c2", "name": "grep", "arguments": {}}]),
        _msg(3, "tool", 1, "files...", tool_call_id="c1"),
    ]
    msgs = build_messages(_ci(1, "isolated", history, ws=WorkspaceView(kind="runner")))
    assert [m.role for m in msgs] == ["user", "assistant", "tool", "tool"]
    assert msgs[2].content == "files..." and "cancelled" in msgs[3].content


def test_build_handoff_includes_plan_and_votes():
    history = HISTORY + [_msg(6, "user", content="build it", recipients=[1], phase="build")]
    ci = _ci(1, "isolated", history, phase="build", votes={3: 1, 5: -1}, ws=WorkspaceView(kind="runner", label="pc:/p", os="Windows 11", shell="powershell", root="C:/p"))
    prompt = system_prompt(ci)
    assert "Phase: BUILD" in prompt and "#3 B ▲ upvoted" in prompt and "B answer" in prompt
    assert "PowerShell" in prompt and "winget" in prompt
    assert "upvoted #3 (B)" in prompt and "downvoted #5 (B)" in prompt
