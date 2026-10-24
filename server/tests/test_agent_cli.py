import asyncio
import json
import os
import sys

import pytest
from multichat_client.runner.agent import Runner

from app.providers.agent_cli.claude_code import ClaudeCodeDriver
from app.providers.agent_cli.base import CliRun
from tests import fake_clis
from tests.conftest import TOKEN, chat, finished


@pytest.fixture
async def cli_env(server, client, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_clis.install(bin_dir, sys.executable)
    log = tmp_path / "calls.jsonl"
    log.touch()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text("print(1)\n")

    r = Runner(server, TOKEN, [str(root)], name=f"cli-{tmp_path.name}")
    task = asyncio.create_task(r.run_forever())
    for _ in range(100):
        if r.runner_id:
            break
        await asyncio.sleep(0.05)
    assert set(r.clis) == {"claude_code", "cursor", "copilot"}

    ids = {}
    for driver, key in (("claude_code", "sk-ant-x"), ("cursor", "cur-key"), ("copilot", None)):
        ep = (
            await client.post(
                "/api/endpoints", json={"name": f"{driver}-{tmp_path.name}", "kind": "agent_cli", "driver": driver, "api_key": key}
            )
        ).json()
        m = (await client.post("/api/models", json={"endpoint_id": ep["id"], "model_name": "default", "display_name": driver})).json()
        ids[driver] = m["id"]
    s = (
        await client.post(
            "/api/sessions",
            json={"title": "cli", "model_ids": list(ids.values()), "workspace": {"kind": "runner", "runner_id": r.runner_id, "path": str(root)}},
        )
    ).json()
    yield s, ids, root, log
    await r.stop()
    task.cancel()


def calls(log, cli):
    return [json.loads(x)["args"] for x in log.read_text().splitlines()
            if json.loads(x)["cli"] == cli and not {"--version", "--list-models"} & set(json.loads(x)["args"])]


async def test_agent_clis_plan_build_resume(client, cli_env):
    s, ids, root, log = cli_env
    replies = {r["model_id"]: r for r in finished(await chat(client, s["id"], "Fix the flaky test"))}
    assert replies[ids["claude_code"]]["content"] == "Claude saw: Fix the flaky test mode=plan"
    assert replies[ids["cursor"]]["content"] == "Cursor plan: Fix the flaky test"
    assert "about: Fix the flaky test" in replies[ids["copilot"]]["content"]
    assert replies[ids["claude_code"]]["tool_calls"][0]["external"] is True

    claude_args = calls(log, "claude")[0]
    assert claude_args[0] == "--bare" and "--resume" not in claude_args
    assert claude_args[claude_args.index("--permission-mode") + 1] == "plan"
    assert "--force" not in calls(log, "cursor")[0]
    assert "--trust" in calls(log, "cursor")[0]  # headless runs can't answer Cursor's trust prompt
    assert calls(log, "copilot")[0][-4:] == ["--deny-tool", "write", "--deny-tool", "shell"]
    assert not (root / "claude_edit.txt").exists() and not (root / "cursor_edit.txt").exists()

    usage = {u["model_id"]: u for u in (await client.get(f"/api/sessions/{s['id']}/usage")).json()}
    assert usage[ids["claude_code"]]["input_tokens"] == 150 and usage[ids["claude_code"]]["cost"] == pytest.approx(0.05)
    assert usage[ids["copilot"]]["estimated"] is True

    # BUILD with Claude Code: command approval goes through the MCP bridge to our approvals API.
    await client.post(f"/api/sessions/{s['id']}/pick-builder", json={"model_id": ids["claude_code"]})

    async def approver():
        async with client.stream("GET", f"/api/sessions/{s['id']}/events") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev["type"] == "approval_required":
                        assert ev["kind"] == "command" and ev["command"] == "make test"
                        await client.post(f"/api/sessions/{s['id']}/approvals/{ev['call_id']}", json={"approve": True})
                        return

    task = asyncio.create_task(approver())
    await asyncio.sleep(0.2)
    events = await chat(client, s["id"], "Go ahead")
    await asyncio.wait_for(task, 10)
    reply = finished(events)[-1]
    assert reply["content"].endswith("mode=acceptEdits decision=allow")
    args = calls(log, "claude")[-1]
    assert args[args.index("--resume") + 1] == "sess-1"
    assert "--permission-prompt-tool" in args
    assert (root / "claude_edit.txt").read_text() == "edited by claude\n"
    changed = [e for e in events if e["type"] == "file_changed"]
    assert [e["path"] for e in changed] == ["claude_edit.txt"]

    usage = {u["model_id"]: u for u in (await client.get(f"/api/sessions/{s['id']}/usage")).json()}
    assert usage[ids["claude_code"]]["cost"] == pytest.approx(0.08)  # 0.05 + (0.08 cumulative - 0.05)

    reverted = (await client.post(f"/api/sessions/{s['id']}/revert", json={})).json()
    assert reverted["restored"] == ["claude_edit.txt"]
    assert not (root / "claude_edit.txt").exists()

    # Cursor as builder auto-applies (with a notice) and resumes its chat.
    await client.post(f"/api/sessions/{s['id']}/pick-builder", json={"model_id": ids["cursor"]})
    events = await chat(client, s["id"], "Your turn")
    assert any(e["type"] == "notice" and "auto-applies" in e["message"] for e in events)
    cargs = calls(log, "cursor")[-1]
    assert "--force" in cargs and "--resume=cur-7" in cargs
    assert (root / "cursor_edit.txt").exists()


async def test_agent_cli_needs_folder(client, cli_env):
    _, ids, _, _ = cli_env
    s = (await client.post("/api/sessions", json={"title": "nofolder", "model_ids": [ids["claude_code"]]})).json()
    reply = finished(await chat(client, s["id"], "hi"))[0]
    assert reply["status"] == "error" and "works inside a folder" in reply["content"]


def test_claude_flags_without_bridge():
    base = dict(prompt="x", model_name="sonnet", api_key=None, resume_id=None, approve_edits="auto")
    inv = ClaudeCodeDriver().build(CliRun(phase="build", approve_commands="ask", bridge_available=False, **base))
    assert "--bare" not in inv.args and ["--model", "sonnet"] == inv.args[inv.args.index("--model") : inv.args.index("--model") + 2]
    assert inv.args[inv.args.index("--permission-mode") + 1] == "acceptEdits" and "--allowedTools" not in inv.args
    assert inv.notices and not inv.use_bridge
    inv = ClaudeCodeDriver().build(CliRun(phase="build", approve_commands="auto", bridge_available=False, **base))
    assert "Bash" in inv.args


async def test_cursor_model_families_effort_and_context(client, cli_env):
    first, ids, root, log = cli_env
    ep_id = next(m["endpoint_id"] for m in (await client.get("/api/models")).json() if m["id"] == ids["cursor"])
    await client.post(f"/api/endpoints/{ep_id}/check")
    by = {m["model_name"]: m for m in (await client.get("/api/models")).json() if m["endpoint_id"] == ep_id}
    assert {"default", "auto", "gpt-5", "claude-opus-4-8"} <= set(by) and "gpt-5-high" not in by  # one model per family
    listed = [json.loads(x) for x in log.read_text().splitlines() if "--list-models" in json.loads(x)["args"]]
    assert listed and listed[-1]["key"] == "cur-key"  # asked with the endpoint's key
    opus, gpt = by["claude-opus-4-8"], by["gpt-5"]
    assert opus["display_name"] == "Cursor · Opus 4.8" and opus["available"]
    assert opus["effort_levels"] == ["medium", "high"] and opus["context_choices"] == [200_000, 1_000_000]
    assert gpt["effort_levels"] == ["low", "medium", "high"] and gpt["context_choices"] is None
    assert by["auto"]["effort_levels"] is None

    s = (await client.post("/api/sessions", json={
        "title": "cursor-variants", "model_ids": [opus["id"]],
        "workspace": {"kind": "runner", "runner_id": first["runner_id"], "path": str(root)},
        "model_settings": {str(opus["id"]): {"context_window": 1_000_000, "effort": "high"}},
    })).json()
    p = s["participants"][0]
    assert p["effort_levels"] == ["medium", "high"] and p["context_choices"] == [200_000, 1_000_000]
    events = await chat(client, s["id"], "Look at main.py")
    assert any(e["type"] == "status" and e["state"] == "starting" and "claude-opus-4-8-1m-thinking-high" in e["text"] for e in events)
    args = calls(log, "cursor")[-1]
    assert args[args.index("--model") + 1] == "claude-opus-4-8-1m-thinking-high"

    # effort changes any time and picks another slug; Cursor's own choices only
    assert (await client.patch(f"/api/sessions/{s['id']}/models/{opus['id']}", json={"effort": "medium"})).status_code == 200
    await chat(client, s["id"], "Again")
    args = calls(log, "cursor")[-1]
    assert args[args.index("--model") + 1] == "claude-opus-4-8-1m"
    assert (await client.patch(f"/api/sessions/{s['id']}/models/{opus['id']}", json={"effort": "low"})).status_code == 422
    assert (await client.patch(f"/api/sessions/{s['id']}/models/{opus['id']}", json={"context_window": 200_000})).status_code == 409


async def test_cli_installed_while_the_runner_runs(server, client, tmp_path, monkeypatch):
    from multichat_client.runner import cli_agents

    bin_dir = tmp_path / "later-bin"
    bin_dir.mkdir()
    monkeypatch.setattr(cli_agents, "known_dirs", lambda: [])
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable))  # no agent CLIs yet
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    ep = (await client.post("/api/endpoints", json={"name": f"cursor-late-{tmp_path.name}", "kind": "agent_cli",
                                                    "driver": "cursor"})).json()
    root = tmp_path / "proj"
    root.mkdir()
    r = Runner(server, TOKEN, [str(root)], name=f"late-{tmp_path.name}")
    seen: list[dict] = []
    r.on_clis = seen.append
    task = asyncio.create_task(r.run_forever())
    for _ in range(100):
        if r.runner_id:
            break
        await asyncio.sleep(0.05)
    assert "cursor" not in r.clis

    fake_clis.install(bin_dir, sys.executable)  # installed now; the app keeps running
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    found = await r.redetect()
    assert "cursor" in found and seen[-1] == found
    for _ in range(100):
        row = next(x for x in (await client.get("/api/runners")).json() if x["id"] == r.runner_id)
        eps = {e["id"]: e for e in (await client.get("/api/endpoints")).json()}
        if "cursor" in (row.get("clis") or {}) and eps[ep["id"]]["status"] == "ok":
            break
        await asyncio.sleep(0.05)
    assert "cursor" in row["clis"] and eps[ep["id"]]["status"] == "ok"
    await r.stop()
    task.cancel()
