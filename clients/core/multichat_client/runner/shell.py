"""Run shell commands on this machine: PowerShell on Windows, bash elsewhere."""

from __future__ import annotations

import asyncio
import codecs
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

OUTPUT_CAP = 200_000
IS_WINDOWS = sys.platform == "win32"


def hidden() -> dict:
    """subprocess kwargs that keep a console window from flashing up when the windowed desktop app starts a
    console program (git, taskkill, `claude --version`, ...). Empty elsewhere."""
    if not IS_WINDOWS:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000), "startupinfo": si}


def default_shell() -> str:
    if IS_WINDOWS:
        return "pwsh" if shutil.which("pwsh") else "powershell"
    return "bash" if shutil.which("bash") else "sh"


def build_argv(command: str, shell: str | None = None) -> list[str]:
    shell = shell or default_shell()
    if shell in ("pwsh", "powershell"):
        exe = "pwsh" if shell == "pwsh" else "powershell.exe"
        # UTF-8 output so the server receives readable text. PowerShell 7.3+: pass arguments to native programs
        # (ssh.exe, git.exe, ...) with standard quoting, so embedded double quotes survive; Windows PowerShell 5.1
        # strips them, which the model is told about (see the server's shell notes).
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$ProgressPreference = 'SilentlyContinue'; "
            + ("$PSNativeCommandArgumentPassing = 'Standard'; " if shell == "pwsh" else "")
            + command
        )
        return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
    if shell == "bash":
        return ["bash", "-lc", command]
    return ["sh", "-c", command]


async def kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False, **hidden())
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def spawn(argv: list[str], cwd: Path, env: dict[str, str] | None = None, stdin: bool = False) -> asyncio.subprocess.Process:
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs = hidden()
        kwargs["creationflags"] |= subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env={**os.environ, **(env or {})},
        # Not DEVNULL: Windows OpenSSH never sends EOF to the remote side for a NUL stdin, so a remote command
        # that reads stdin (e.g. `grep pattern` without a file) waits until the timeout. A pipe that is closed
        # right away gives every program an immediate end of input.
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if not stdin else asyncio.subprocess.PIPE,
        **kwargs,
    )
    if not stdin and proc.stdin is not None:
        proc.stdin.close()
    return proc


async def run(
    command: str,
    cwd: Path,
    timeout: float,
    on_output: Callable[[str], Awaitable[None]] | None = None,
    shell: str | None = None,
) -> dict:
    proc = await spawn(build_argv(command, shell), cwd)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    chunks: list[str] = []
    size = 0

    async def pump() -> None:
        nonlocal size
        assert proc.stdout is not None
        while True:
            data = await proc.stdout.read(4096)
            if not data:
                break
            text = decoder.decode(data)
            if not text:
                continue
            size += len(text)
            chunks.append(text)
            if on_output:
                await on_output(text)

    timed_out = False
    try:
        await asyncio.wait_for(pump(), timeout)
        await proc.wait()
    except TimeoutError:
        timed_out = True
        await kill_tree(proc)
    except asyncio.CancelledError:
        await kill_tree(proc)
        raise
    output = "".join(chunks)
    if len(output) > OUTPUT_CAP:
        half = OUTPUT_CAP // 2
        output = output[:half] + f"\n… ({len(output) - OUTPUT_CAP} characters omitted) …\n" + output[-half:]
    return {"exit_code": proc.returncode, "timed_out": timed_out, "output": output}
