"""Launch agent CLIs (Claude Code, Cursor, GitHub Copilot) that the user installed on this machine."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from multichat_client.runner import shell

IS_WINDOWS = sys.platform == "win32"

EXECUTABLES = {
    "claude_code": ["claude"],
    "cursor": ["cursor-agent", "agent"],
    "copilot": ["copilot"],
}


# Windows launchers in the order a terminal would pick them; an extensionless file there is a shell script for Git
# Bash, not something Windows can start. A .ps1 launcher is started through powershell.exe (script policy bypassed).
WINDOWS_EXTS = (".exe", ".cmd", ".bat", ".ps1")


def registry_path() -> list[str]:
    """The user's and the system's PATH as saved now. A program that was already running when an installer added a
    folder (the desktop app sits in the tray) still has the old PATH in its environment."""
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []
    keys = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    out: list[str] = []
    for root, key in keys:
        try:
            with winreg.OpenKey(root, key) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
        except OSError:
            continue
        out += [os.path.expandvars(p) for p in str(value).split(";") if p.strip()]
    return out


def known_dirs() -> list[str]:
    """Where the official installers put the CLIs, in case PATH doesn't (yet) include them."""
    home = Path.home()
    dirs = [home / ".local" / "bin"]  # Cursor (Linux/macOS) and Claude Code native installers
    if IS_WINDOWS:
        if os.environ.get("LOCALAPPDATA"):
            dirs.append(Path(os.environ["LOCALAPPDATA"]) / "cursor-agent")  # irm 'https://cursor.com/install?win32=true'
        if os.environ.get("APPDATA"):
            dirs.append(Path(os.environ["APPDATA"]) / "npm")  # npm install -g (claude, copilot)
    return [str(d) for d in dirs]


def search_path() -> list[str]:
    """Folders to look in: this process's PATH, then (Windows) the saved PATH, then the installers' folders."""
    seen: set[str] = set()
    out: list[str] = []
    for d in [*os.environ.get("PATH", "").split(os.pathsep), *(registry_path() if IS_WINDOWS else []), *known_dirs()]:
        key = os.path.normcase(os.path.normpath(d)) if d else ""
        if key and key not in seen:
            seen.add(key)
            out.append(d)
    return out


def find_in(names: list[str], dirs: list[str], windows: bool) -> str | None:
    """First match: names in order of preference (cursor-agent before agent), then folders, then extensions."""
    for name in names:
        for d in dirs:
            if windows:
                for ext in WINDOWS_EXTS:
                    cand = os.path.join(d, name + ext)
                    if os.path.isfile(cand):
                        return cand
            else:
                cand = os.path.join(d, name)
                if os.path.isfile(cand) and os.access(cand, os.X_OK):
                    return cand
    return None


def find(driver: str) -> str | None:
    return find_in(EXECUTABLES.get(driver, []), search_path(), IS_WINDOWS)


def command(exe: str) -> list[str]:
    """argv prefix that starts `exe` (a .ps1 launcher needs powershell.exe)."""
    if exe.lower().endswith(".ps1"):
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", exe]
    return [exe]


def child_env(env: dict[str, str]) -> dict[str, str]:
    """The CLI's environment: the saved PATH too, so its launcher finds its own runtime (node, versions folder)."""
    return {"PATH": os.pathsep.join(search_path()), **env}


_versions: dict[tuple[str, float], str] = {}  # (path, mtime) -> version, so re-checks don't start processes


def version_of(exe: str) -> str:
    try:
        key = (exe, os.path.getmtime(exe))
    except OSError:
        return ""
    if key not in _versions:
        try:
            r = subprocess.run([*command(exe), "--version"], capture_output=True, text=True, timeout=15, check=False,
                               env={**os.environ, **child_env({})}, **shell.hidden())
            out = (r.stdout or r.stderr).strip()
            _versions[key] = out.splitlines()[0] if out else ""
        except (OSError, subprocess.TimeoutExpired):
            _versions[key] = ""
    return _versions[key]


def detect() -> dict[str, dict]:
    """Which agent CLIs are installed here, with versions (reported to the server on connect and when it changes)."""
    found: dict[str, dict] = {}
    for driver in EXECUTABLES:
        exe = find(driver)
        if exe:
            found[driver] = {"path": exe, "version": version_of(exe)}
    return found


async def list_models(driver: str, env: dict[str, str], cwd: Path) -> dict:
    """The CLI's own model list (`cursor-agent --list-models`); the server parses the text."""
    exe = find(driver)
    if exe is None:
        raise FileNotFoundError(f"{' or '.join(EXECUTABLES.get(driver, [driver]))} is not installed on this machine")
    if driver != "cursor":
        return {"output": "", "exit_code": 0}
    last: dict = {}
    for args in (["--list-models"], ["models"]):
        proc = await shell.spawn([*command(exe), *args], cwd, child_env(env))
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 40)
        except TimeoutError:
            await shell.kill_tree(proc)
            raise
        last = {"output": out.decode("utf-8", errors="replace"), "exit_code": proc.returncode}
        if proc.returncode == 0 and last["output"].strip():
            break
    return last


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
    if IS_WINDOWS and exe.lower().endswith((".cmd", ".bat", ".ps1")):
        args, prompt_file = _args_for_cmd_wrapper(args, cwd)
    argv = [*command(exe), *args]
    if approval and driver == "claude_code":
        mcp_path = write_mcp_config(server_url, token, approval["session_id"], approval["model_id"])
        argv += ["--mcp-config", mcp_path, "--permission-prompt-tool", "mcp__multichat__approve"]
    proc = await shell.spawn(argv, cwd, child_env(env), stdin=True)
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
