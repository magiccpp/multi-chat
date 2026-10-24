import json
import subprocess
import sys

import pytest

from multichat_client.runner import approval_mcp, fs, shell, snapshot
from multichat_client.runner.fs import FsError, Roots
from multichat_client.state import SessionState


@pytest.mark.parametrize("use_git", [False, True])
def test_snapshot_detects_changes(tmp_path, monkeypatch, use_git):
    monkeypatch.setattr(snapshot, "CACHE", tmp_path / "cache")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("one\n")
    (root / "gone.txt").write_text("bye\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.js").write_text("x")
    if use_git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    ref = snapshot.begin(root)
    assert ref.startswith("git:" if use_git else "copy:")
    (root / "a.txt").write_text("two\n")
    (root / "gone.txt").unlink()
    (root / "new.txt").write_text("hi\n")
    changes = {c["path"]: c for c in snapshot.changes(root, ref)}
    assert changes["a.txt"] == {"path": "a.txt", "before": "one\n", "after": "two\n"}
    assert changes["gone.txt"]["after"] is None and changes["gone.txt"]["before"] == "bye\n"
    assert changes["new.txt"]["before"] is None
    assert not any("node_modules" in p for p in changes)


def test_roots_confinement(tmp_path):
    (tmp_path / "w").mkdir()
    roots = Roots([str(tmp_path / "w")])
    assert roots.join(str(tmp_path / "w"), "x/y.txt") == (tmp_path / "w" / "x" / "y.txt").resolve()
    with pytest.raises(FsError):
        roots.join(str(tmp_path / "w"), "../escape")
    with pytest.raises(FsError):
        roots.check(str(tmp_path))
    (tmp_path / "w" / "link").symlink_to(tmp_path)
    with pytest.raises(FsError):
        roots.join(str(tmp_path / "w"), "link/other")


def test_fs_read_write_grep(tmp_path):
    fs.write(tmp_path / "d" / "f.py", "a = 1\nb = 2\r\n")
    assert (tmp_path / "d" / "f.py").read_bytes() == b"a = 1\nb = 2\r\n"
    assert fs.read(tmp_path / "missing") == {"exists": False}
    (tmp_path / "bin").write_bytes(b"\0\1\2")
    assert fs.read(tmp_path / "bin")["binary"]
    out = fs.grep(tmp_path, tmp_path, r"b = \d", "*.py", 10)
    assert out == "d/f.py:2:b = 2"


def test_powershell_argv():
    argv = shell.build_argv("winget install git", "powershell")
    assert argv[0] == "powershell.exe" and "-NonInteractive" in argv and argv[-1].endswith("winget install git")
    assert "UTF8" in argv[-1]
    assert shell.build_argv("ls", "bash") == ["bash", "-lc", "ls"]


async def test_shell_run_timeout_and_output(tmp_path):
    seen = []

    async def on_output(text):
        seen.append(text)

    r = await shell.run("echo hi; sleep 5", tmp_path, 0.5, on_output)
    assert r["timed_out"] and "hi" in "".join(seen)
    r = await shell.run("exit 3", tmp_path, 5)
    assert r["exit_code"] == 3


def test_approval_mcp_protocol(monkeypatch):
    init = approval_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert approval_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = approval_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert tools["result"]["tools"][0]["name"] == "approve"
    monkeypatch.setattr(approval_mcp, "decide", lambda args: {"behavior": "allow", "updatedInput": args["input"]})
    call = approval_mcp.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "approve", "arguments": {"tool_name": "Bash", "input": {"command": "ls"}}}}
    )
    assert json.loads(call["result"]["content"][0]["text"]) == {"behavior": "allow", "updatedInput": {"command": "ls"}}


def test_approval_mcp_stdio_roundtrip():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "multichat_client.runner.approval_mcp"],
        input="\n".join(json.dumps(x) for x in lines) + "\n",
        capture_output=True,
        text=True,
        timeout=20,
    )
    replies = [json.loads(x) for x in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]


def test_session_state_reducer():
    st = SessionState()
    st.load(
        {"id": 1, "participants": [{"model_id": 7}], "busy": False},
        [{"id": 1, "seq": 1, "role": "user", "model_id": None, "content": "hi", "recipients": [7]}],
        [],
        [{"id": 7, "display_name": "Alpha", "color": "#123456"}],
    )
    st.apply({"type": "turn_start"})
    st.apply({"type": "message_start", "message": {"id": 2, "seq": 2, "role": "assistant", "model_id": 7, "content": ""}})
    st.apply({"type": "delta", "message_id": 2, "text": "Hel"})
    st.apply({"type": "delta", "message_id": 2, "text": "lo"})
    assert st.messages[2].content == "Hello" and st.busy
    st.apply({"type": "usage", "model_id": 7, "input_tokens": 10, "output_tokens": 5, "cost": 0.01, "estimated": False})
    st.apply({"type": "vote", "message_id": 2, "value": 1})
    st.apply({"type": "message_end", "message": {"id": 2, "seq": 2, "role": "assistant", "model_id": 7, "content": "Hello"}})
    st.apply({"type": "turn_end"})
    assert st.messages[2].vote == 1  # kept across message_end
    assert st.usage[7].cost == 0.01 and not st.busy
    assert [m.id for m in st.ordered(7)] == [1, 2]
    assert st.by_seq(2).id == 2 and st.latest_reply(7).id == 2
