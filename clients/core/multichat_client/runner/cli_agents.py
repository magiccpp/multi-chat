"""Launch agent CLIs (Claude Code, Cursor, GitHub Copilot) that the user installed on this machine."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from multichat_client.runner import shell

EXECUTABLES = {
    "claude_code": ["claude"],
    "cursor": ["cursor-agent", "agent"],
    "copilot": ["copilot"],
}


def find(driver: str) -> str | None:
    for name in EXECUTABLES.get(driver, []):
        path = shutil.which(name)
        if path:
            return path
    return None


def detect() -> dict[str, dict]:
    """Which agent CLIs are installed here, with versions (reported to the server on connect)."""
    found: dict[str, dict] = {}
    for driver in EXECUTABLES:
        exe = find(driver)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, check=False)
            version = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""
        except (OSError, subprocess.TimeoutExpired):
            version = ""
        found[driver] = {"path": exe, "version": version}
    return found


def approval_bridge_command() -> list[str]:
    """How Claude Code should start our approval MCP server (works from source and from the frozen .exe)."""
    if getattr(sys, "frozen", False):
        bridge = Path(sys.executable).with_name("multichat-approval-bridge.exe")
        if bridge.exists():
            return [str(bridge)]
        return [sys.executable, "--approval-mcp"]
    return [sys.executable, "-m", "multichat_client.runner.approval_mcp"]


def write_mcp_config(server_url: str, token: str, session_id: int, model_id: int) -> str:
    command, *args = approval_bridge_command()
    config = {
        "mcpServers": {
            "multichat": {
                "type": "stdio",
                "command": command,
                "args": args,
                "env": {
                    "MULTICHAT_URL": server_url,
                    "MULTICHAT_TOKEN": token,
                    "MULTICHAT_SESSION_ID": str(session_id),
                    "MULTICHAT_MODEL_ID": str(model_id),
                },
            }
        }
    }
    fd, path = tempfile.mkstemp(prefix="multichat-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


async def run(
    driver: str,
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    stdin: str | None,
    on_line: Callable[[str, bool], Awaitable[None]],
    *,
    approval: dict | None = None,
    server_url: str = "",
    token: str = "",
) -> int:
    exe = find(driver)
    if exe is None:
        names = " or ".join(EXECUTABLES.get(driver, [driver]))
        raise FileNotFoundError(f"{names} is not installed on this machine (or not on PATH)")
    mcp_path = None
    prompt_file = None
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        args, prompt_file = _args_for_cmd_wrapper(args, cwd)
    argv = [exe, *args]
    if approval and driver == "claude_code":
        mcp_path = write_mcp_config(server_url, token, approval["session_id"], approval["model_id"])
        argv += ["--mcp-config", mcp_path, "--permission-prompt-tool", "mcp__multichat__approve"]
    proc = await shell.spawn(argv, cwd, env, stdin=True)
    try:
        assert proc.stdin and proc.stdout and proc.stderr
        if stdin:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
        proc.stdin.close()

        async def pump(stream: asyncio.StreamReader, is_err: bool) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                await on_line(line.decode("utf-8", errors="replace").rstrip("\r\n"), is_err)

        await asyncio.gather(pump(proc.stdout, False), pump(proc.stderr, True))
        return await proc.wait()
    except asyncio.CancelledError:
        await shell.kill_tree(proc)
        raise
    finally:
        if mcp_path:
            os.unlink(mcp_path)
        if prompt_file:
            prompt_file.unlink(missing_ok=True)


def _args_for_cmd_wrapper(args: list[str], cwd: Path) -> tuple[list[str], Path | None]:
    """cmd.exe cannot pass multi-line or quote-heavy arguments to .cmd shims (npm installs); move the prompt to a file."""
    out, prompt_file = [], None
    for arg in args:
        if prompt_file is None and (len(arg) > 200 or any(ch in arg for ch in '\n"^&|<>%')):
            folder = cwd / ".multichat"
            folder.mkdir(exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="prompt-", suffix=".md", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(arg)
            prompt_file = Path(name)
            arg = f"Your task is in the file .multichat/{prompt_file.name} - read it first and follow it."
        out.append(arg)
    return out, prompt_file
