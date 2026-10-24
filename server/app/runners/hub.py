"""Registry of connected runners and request/response RPC over their WebSockets."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app import db as dbmod
from app.tables import Runner

log = logging.getLogger(__name__)
StreamCallback = Callable[[dict[str, Any]], Awaitable[None]]


class RunnerOffline(Exception):
    pass


class RunnerError(Exception):
    pass


class RunnerConnection:
    def __init__(self, ws: WebSocket, runner_id: int, hello: dict[str, Any]) -> None:
        self.ws = ws
        self.runner_id = runner_id
        self.hello = hello
        self._ids = itertools.count(1)
        self._pending: dict[str, tuple[asyncio.Future, StreamCallback | None]] = {}
        self._send_lock = asyncio.Lock()
        self.closed = False

    @property
    def os(self) -> str:
        return self.hello.get("os", "")

    async def send(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_json(message)

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        on_stream: StreamCallback | None = None,
        timeout: float | None = 120,
    ) -> dict[str, Any]:
        if self.closed:
            raise RunnerOffline("runner disconnected")
        rid = str(next(self._ids))
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = (future, on_stream)
        try:
            await self.send({"type": "request", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except (asyncio.CancelledError, TimeoutError):
            if not self.closed:
                try:
                    await self.send({"type": "cancel", "id": rid})
                except Exception:  # noqa: BLE001 - best effort
                    pass
            raise
        finally:
            self._pending.pop(rid, None)

    async def handle(self, message: dict[str, Any]) -> None:
        mtype = message.get("type")
        if mtype == "ping":
            await self.send({"type": "pong"})
            return
        entry = self._pending.get(str(message.get("id")))
        if entry is None:
            return
        future, on_stream = entry
        if mtype == "stream" and on_stream is not None:
            await on_stream(message.get("data") or {})
        elif mtype == "response" and not future.done():
            if message.get("error"):
                future.set_exception(RunnerError(message["error"]))
            else:
                future.set_result(message.get("result") or {})

    def fail_all(self) -> None:
        self.closed = True
        for future, _ in self._pending.values():
            if not future.done():
                future.set_exception(RunnerOffline("runner disconnected"))


class RunnerHub:
    def __init__(self) -> None:
        self.connections: dict[int, RunnerConnection] = {}

    def get(self, runner_id: int) -> RunnerConnection:
        conn = self.connections.get(runner_id)
        if conn is None or conn.closed:
            raise RunnerOffline("the runner for this session is offline")
        return conn

    def online(self, runner_id: int) -> bool:
        conn = self.connections.get(runner_id)
        return conn is not None and not conn.closed

    async def serve(self, ws: WebSocket) -> None:
        hello = await ws.receive_json()
        if hello.get("type") != "hello" or not hello.get("name"):
            await ws.close(code=4400)
            return
        async with dbmod.sessionmaker()() as db:
            row = (await db.execute(select(Runner).where(Runner.name == hello["name"]))).scalar_one_or_none()
            if row is None:
                row = Runner(name=hello["name"])
                db.add(row)
            row.os = hello.get("os", "")
            row.shell = hello.get("shell", "")
            row.hostname = hello.get("hostname", "")
            row.roots = list(hello.get("roots") or [])
            row.clis = dict(hello.get("clis") or {})
            row.version = str(hello.get("version", ""))
            row.last_seen = datetime.now(UTC)
            await db.commit()
            runner_id = row.id

        old = self.connections.get(runner_id)
        if old is not None:
            old.fail_all()
            try:
                await old.ws.close(code=4409)
            except Exception:  # noqa: BLE001
                pass
        conn = RunnerConnection(ws, runner_id, hello)
        self.connections[runner_id] = conn
        await conn.send({"type": "welcome", "runner_id": runner_id})
        log.info("runner %s (%s) connected", hello["name"], runner_id)
        try:
            while True:
                message = await ws.receive_json()
                # Streams and responses are handled inline; handlers never block on the socket.
                await conn.handle(message)
        except WebSocketDisconnect:
            pass
        finally:
            conn.fail_all()
            if self.connections.get(runner_id) is conn:
                del self.connections[runner_id]
            async with dbmod.sessionmaker()() as db:
                row = await db.get(Runner, runner_id)
                if row is not None:
                    row.last_seen = datetime.now(UTC)
                    await db.commit()
            log.info("runner %s disconnected", hello["name"])


hub = RunnerHub()
