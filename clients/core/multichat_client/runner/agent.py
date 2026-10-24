"""The local runner: connects to the server and executes file/command requests on this machine."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from multichat_client import __version__
from multichat_client.runner import cli_agents, fs, shell, snapshot
from multichat_client.runner.fs import FsError, Roots

log = logging.getLogger("multichat.runner")
ConfirmCallback = Callable[[str], Awaitable[bool]]
StatusCallback = Callable[[str, str], None]  # (state, detail): connecting | online | offline | error


def ws_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url[8:] + "/api/runners/connect"
    if http_url.startswith("http://"):
        return "ws://" + http_url[7:] + "/api/runners/connect"
    return http_url


def os_name() -> str:
    """ "Windows 11", "Linux 6.8.0" … without platform.release() on Windows, which may run `cmd /c ver` and flash a
    console window from the windowed desktop app."""
    if sys.platform == "win32":
        v = sys.getwindowsversion()
        return f"Windows {'11' if v.major == 10 and v.build >= 22000 else v.major}"
    return f"{platform.system()} {platform.release()}".strip()


class Runner:
    def __init__(
        self,
        url: str,
        token: str,
        roots: list[str],
        *,
        name: str | None = None,
        confirm_command: ConfirmCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> None:
        if not roots:
            raise ValueError("a runner needs at least one allowed folder")
        self.url = url.rstrip("/")
        self.token = token
        self.roots = Roots(roots)
        self.name = name or socket.gethostname()
        self.shell = shell.default_shell()
        self.confirm_command = confirm_command
        self.on_status = on_status or (lambda state, detail: None)
        self.runner_id: int | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._ws: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._stopping = False
        self.clis: dict[str, dict] = {}
        self.on_clis: Callable[[dict[str, dict]], None] = lambda clis: None  # the apps show what was found

    def hello(self) -> dict[str, Any]:
        return {
            "type": "hello",
            "name": self.name,
            "os": os_name(),
            "shell": self.shell,
            "hostname": socket.gethostname(),
            "roots": [str(r) for r in self.roots.roots],
            "clis": self.clis,
            "version": __version__,
        }

    REDETECT_EVERY = 120.0  # seconds: a CLI installed while the app runs shows up without a restart

    async def redetect(self) -> dict[str, dict]:
        """Look for agent CLIs again; tell the server when something changed."""
        clis = await asyncio.to_thread(cli_agents.detect)
        if clis != self.clis:
            self.clis = clis
            await self._send({"type": "clis", "clis": clis})
        self.on_clis(clis)
        return clis

    async def _redetect_loop(self) -> None:
        while True:
            await asyncio.sleep(self.REDETECT_EVERY)
            try:
                await self.redetect()
            except Exception:  # noqa: BLE001 - never let a failed check stop the runner
                log.exception("agent CLI re-check failed")

    async def run_forever(self) -> None:
        delay = 1.0
        while not self._stopping:
            self.clis = await asyncio.to_thread(cli_agents.detect)
            self.on_clis(self.clis)
            self.on_status("connecting", self.url)
            try:
                async with connect(
                    ws_url(self.url),
                    additional_headers={"Authorization": f"Bearer {self.token}"},
                    max_size=64 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=60,
                ) as ws:
                    delay = 1.0
                    await self._serve(ws)
            except InvalidStatus as exc:
                self.on_status("error", f"server refused the runner: HTTP {exc.response.status_code}")
            except (OSError, ConnectionClosed, TimeoutError) as exc:
                if self._stopping:
                    break
                code = getattr(exc, "rcvd", None) and exc.rcvd.code  # type: ignore[union-attr]
                if code == 4401:
                    self.on_status("error", "invalid API token")
                elif code == 4409:
                    self.on_status("offline", "another runner with the same name connected")
                else:
                    self.on_status("offline", str(exc) or type(exc).__name__)
            if self._stopping:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        self.on_status("offline", "stopped")

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks.values()):
            task.cancel()
        if self._ws is not None:
            await self._ws.close()

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send(json.dumps(message))

    async def _serve(self, ws: ClientConnection) -> None:
        self._ws = ws
        await ws.send(json.dumps(self.hello()))
        welcome = json.loads(await ws.recv())
        self.runner_id = welcome.get("runner_id")
        self.on_status("online", f"connected as {self.name}")
        recheck = asyncio.create_task(self._redetect_loop())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                mtype = msg.get("type")
                if mtype == "request":
                    rid = str(msg["id"])
                    self._tasks[rid] = asyncio.create_task(self._handle(rid, msg["method"], msg.get("params") or {}))
                elif mtype == "cancel":
                    task = self._tasks.get(str(msg.get("id")))
                    if task:
                        task.cancel()
                elif mtype == "ping":
                    await self._send({"type": "pong"})
        finally:
            recheck.cancel()
            for task in list(self._tasks.values()):
                task.cancel()
            self._ws = None

    async def _handle(self, rid: str, method: str, params: dict[str, Any]) -> None:
        async def stream(data: dict[str, Any]) -> None:
            await self._send({"type": "stream", "id": rid, "data": data})

        try:
            result = await self.dispatch(method, params, stream)
            await self._send({"type": "response", "id": rid, "result": result})
        except asyncio.CancelledError:
            pass
        except (FsError, OSError, ValueError, KeyError) as exc:
            await self._send({"type": "response", "id": rid, "error": f"{type(exc).__name__}: {exc}" if not isinstance(exc, FsError) else str(exc)})
        except Exception as exc:  # noqa: BLE001 - report, never crash the runner
            log.exception("runner request %s failed", method)
            await self._send({"type": "response", "id": rid, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            self._tasks.pop(rid, None)

    async def dispatch(self, method: str, p: dict[str, Any], stream: Callable[[dict], Awaitable[None]]) -> dict[str, Any]:
        if method == "fs.browse":
            if not p.get("path"):
                return {"path": "", "entries": [{"name": str(r), "is_dir": True} for r in self.roots.roots]}
            target = self.roots.check(p["path"])
            entries = [e for e in await asyncio.to_thread(fs.list_dir, target) if e["is_dir"]]
            return {"path": str(target), "entries": entries}
        root = p.get("root", "")
        if method == "fs.list":
            return {"entries": await asyncio.to_thread(fs.list_dir, self.roots.join(root, p.get("path", ".")))}
        if method == "fs.read":
            return await asyncio.to_thread(fs.read, self.roots.join(root, p["path"]))
        if method == "fs.write":
            await asyncio.to_thread(fs.write, self.roots.join(root, p["path"]), p["content"])
            return {}
        if method == "fs.delete":
            await asyncio.to_thread(fs.delete, self.roots.join(root, p["path"]))
            return {}
        if method == "fs.grep":
            base = self.roots.join(root, ".")
            target = self.roots.join(root, p.get("path") or ".")
            out = await asyncio.to_thread(fs.grep, base, target, p["pattern"], p.get("glob"), int(p.get("max_results", 200)))
            return {"output": out}
        if method == "shell.run":
            cwd = self.roots.join(root, ".")
            if self.confirm_command is not None and not await self.confirm_command(p["command"]):
                return {"exit_code": None, "timed_out": False, "output": "Blocked: the user declined this command on the runner machine."}
            return await shell.run(
                p["command"], cwd, float(p.get("timeout", 600)), lambda text: stream({"text": text}), self.shell
            )
        if method == "snapshot.begin":
            return {"ref": await asyncio.to_thread(snapshot.begin, self.roots.join(root, "."))}
        if method == "snapshot.changes":
            return {"changes": await asyncio.to_thread(snapshot.changes, self.roots.join(root, "."), p["ref"])}
        if method == "agent_cli.run":
            cwd = self.roots.join(root, ".")

            async def on_line(line: str, is_err: bool) -> None:
                await stream({"line": line, "stderr": is_err})

            code = await cli_agents.run(
                p["driver"],
                list(p.get("args") or []),
                cwd,
                dict(p.get("env") or {}),
                p.get("stdin"),
                on_line,
                approval=p.get("approval"),
                server_url=self.url,
                token=self.token,
            )
            return {"exit_code": code}
        if method == "agent_cli.models":
            return await cli_agents.list_models(p["driver"], dict(p.get("env") or {}), Path.home())
        raise ValueError(f"unknown method {method}")


def resolve_roots(roots: list[str]) -> list[str]:
    return [str(Path(r).expanduser().resolve()) for r in roots]
