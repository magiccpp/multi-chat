"""Holds the API client, the current session state and the local runner; emits Qt signals for the UI."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

from multichat_client.client import ApiError, MultiChatClient
from multichat_client.runner.agent import Runner
from multichat_client.state import SessionState
from multichat_desktop.asyncutil import spawn

log = logging.getLogger("multichat.desktop")


class Controller(QObject):
    session_loaded = Signal()
    event = Signal(dict)
    runner_status = Signal(str, str)
    models_changed = Signal()
    error = Signal(str)

    def __init__(self, client: MultiChatClient) -> None:
        super().__init__()
        self.client = client
        self.st = SessionState()
        self.models: list[dict] = []
        self.runner: Runner | None = None
        self.runner_loop: asyncio.AbstractEventLoop | None = None
        self.runner_thread: threading.Thread | None = None
        self.runner_state = "off"
        self._events_task: asyncio.Task | None = None
        self.recipients: list[int] | None = None

    # ---- models & sessions ----
    async def refresh_models(self) -> None:
        self.models = await self.client.models()
        self.st.models = {m["id"]: m for m in self.models}
        self.models_changed.emit()

    async def open_session(self, session_id: int) -> None:
        if self._events_task:
            self._events_task.cancel()
        session, messages, usage = await asyncio.gather(
            self.client.session(session_id), self.client.messages(session_id), self.client.usage(session_id)
        )
        await self.refresh_models()
        st = SessionState()
        st.load(session, messages, usage, self.models)
        try:
            st.file_changes = [
                {"path": c["path"], "turn_id": c["turn_id"], "reverted": c["reverted"], "diff": ""}
                for c in await self.client.changes(session_id)
            ]
        except ApiError:
            pass
        self.st = st
        self.recipients = None
        self.session_loaded.emit()
        self._events_task = asyncio.ensure_future(self._follow(session_id))

    async def open_latest_or_new(self) -> None:
        sessions = await self.client.sessions(limit=1)
        sid = sessions[0]["id"] if sessions else (await self.client.create_session("New session"))["id"]
        await self.open_session(sid)

    async def new_session(self, title: str = "New session") -> None:
        ids = [p["model_id"] for p in self.st.participants]
        s = await self.client.create_session(title, ids, self.st.session.get("context_mode", "isolated"))
        await self.open_session(s["id"])

    async def _follow(self, session_id: int) -> None:
        while True:
            try:
                async for ev in self.client.events(session_id):
                    self.st.apply(ev)
                    self.event.emit(ev)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect after network errors
                await asyncio.sleep(2)

    def set_session(self, session: dict) -> None:
        self.st.session = session
        self.event.emit({"type": "session", "session": session})

    # ---- actions ----
    def send(self, text: str) -> None:
        async def go() -> None:
            async for _ in self.client.chat(self.st.session_id, text, self.recipients):
                pass  # events arrive through the session subscription

        spawn(go(), lambda exc: self.error.emit(getattr(exc, "detail", str(exc))))

    def call(self, coro, on_done=None) -> None:  # noqa: ANN001
        async def go() -> None:
            result = await coro
            if on_done:
                on_done(result)

        spawn(go(), lambda exc: self.error.emit(getattr(exc, "detail", str(exc))))

    def vote(self, message_id: int, value: int) -> None:
        def done(_: Any) -> None:
            m = self.st.messages.get(message_id)
            if m:
                m.vote = value
            self.event.emit({"type": "vote", "message_id": message_id, "value": value})

        self.call(self.client.vote(message_id, value), done)

    def pick_builder(self, model_id: int) -> None:
        self.call(self.client.pick_builder(self.st.session_id, model_id), self.set_session)

    def update_session(self, **fields: Any) -> None:
        self.call(self.client.update_session(self.st.session_id, **fields), self.set_session)

    def set_models(self, model_ids: list[int]) -> None:
        self.call(self.client.set_session_models(self.st.session_id, model_ids), self.set_session)

    def approve(self, call_id: str, approve: bool, note: str | None = None) -> None:
        self.st.approvals.pop(call_id, None)
        self.call(self.client.approve(self.st.session_id, call_id, approve, note))

    def stop(self) -> None:
        self.call(self.client.stop(self.st.session_id))

    def revert(self, turn_id: str | None = None) -> None:
        def done(result: dict) -> None:
            for ch in self.st.file_changes:
                if ch["turn_id"] == result["turn_id"]:
                    ch["reverted"] = True
            self.event.emit({"type": "reverted", **result})

        self.call(self.client.revert(self.st.session_id, turn_id), done)

    # ---- runner ----
    # The runner lives on its own thread with a plain asyncio loop: on Windows that is a Proactor loop, which
    # supports subprocesses (PowerShell commands, agent CLIs) independently of the Qt-integrated loop.
    def start_runner(self, roots: list[str], name: str | None, confirm=None) -> None:  # noqa: ANN001
        self.stop_runner()
        gui_loop = asyncio.get_event_loop()

        def status(state: str, detail: str) -> None:
            self.runner_state = state
            self.runner_status.emit(state, detail)  # Qt queues cross-thread signal emissions

        async def confirm_on_gui(command: str) -> bool:
            # called on the runner thread; the dialog must run on the GUI loop
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(confirm(command), gui_loop))

        runner = Runner(
            self.client.url, self.client.token, roots, name=name,
            confirm_command=confirm_on_gui if confirm else None, on_status=status,
        )
        started = threading.Event()

        def run() -> None:
            loop = asyncio.new_event_loop()
            self.runner_loop = loop
            asyncio.set_event_loop(loop)
            started.set()
            try:
                loop.run_until_complete(runner.run_forever())
            except Exception:  # noqa: BLE001
                log.exception("runner stopped")
            finally:
                loop.close()

        self.runner = runner
        self.runner_thread = threading.Thread(target=run, name="multichat-runner", daemon=True)
        self.runner_thread.start()
        started.wait(5)

    def stop_runner(self) -> None:
        runner, loop, thread = self.runner, self.runner_loop, self.runner_thread
        self.runner = self.runner_loop = self.runner_thread = None
        if runner and loop and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(runner.stop(), loop)
            except RuntimeError:
                pass
        if thread:
            thread.join(5)
        self.runner_state = "off"
        self.runner_status.emit("off", "")

    async def shutdown(self) -> None:
        if self._events_task:
            self._events_task.cancel()
        await asyncio.to_thread(self.stop_runner)
        await self.client.close()
