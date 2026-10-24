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
    notice = Signal(str)  # plain status-bar messages
    clis_changed = Signal(object)  # agent CLIs the runner found on this PC: {driver: {path, version}}

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
        self.attachment_cache: dict[int, bytes] = {}

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
        await self.attach_this_pc()

    async def open_latest_or_new(self) -> None:
        sessions = await self.client.sessions(limit=1)
        sid = sessions[0]["id"] if sessions else (await self.client.create_session("New session"))["id"]
        await self.open_session(sid)

    async def new_session(self, title: str = "New session") -> bool:
        """Start a new session with the current (still available) models. Returns False and does nothing when the
        current session is still empty — there is no point in piling up blank sessions."""
        if self.st.is_empty:
            return False
        await self.refresh_models()
        available = {m["id"] for m in self.models if m["available"] and m["enabled"]}
        ids = [p["model_id"] for p in self.st.participants if p["model_id"] in available]
        s = await self.client.create_session(title, ids, self.st.session.get("context_mode", "isolated"), reuse_empty=True)
        await self.open_session(s["id"])
        return True

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

    async def attach_this_pc(self) -> bool:
        """A session without a folder uses this PC while the runner is online, so models can inspect and work on it."""
        from multichat_desktop import settings

        s, runner = self.st.session, self.runner
        if (not s or s.get("workspace_kind") != "none" or runner is None or not runner.runner_id
                or self.runner_state != "online" or not settings.runner_auto_attach()
                or s["id"] in settings.no_folder_sessions() or not runner.roots.roots):
            return False
        updated = await self.client.set_workspace(s["id"], "runner", str(runner.roots.roots[0]), runner_id=runner.runner_id)
        if self.st.session.get("id") == updated["id"]:
            self.set_session(updated)
        return True

    def set_session(self, session: dict) -> None:
        self.st.session = session
        self.event.emit({"type": "session", "session": session})

    # ---- actions ----
    def send(self, text: str, attachments: list | None = None) -> None:
        """Upload the attachments (PendingAttachment), then start the turn with their ids."""
        session_id = self.st.session_id

        async def go() -> None:
            ids = []
            for att in attachments or []:
                up = await self.client.upload_attachment(session_id, att.filename, att.data, att.mime)
                if up["kind"] != "image":
                    self.attachment_cache[up["id"]] = att.data  # images come back resized; fetch those
                ids.append(up["id"])
            async for _ in self.client.chat(session_id, text, self.recipients, ids):
                pass  # events arrive through the session subscription

        spawn(go(), lambda exc: self.error.emit(getattr(exc, "detail", str(exc))))

    def fetch_attachment(self, attachment_id: int, on_done) -> None:  # noqa: ANN001
        if attachment_id in self.attachment_cache:
            on_done(self.attachment_cache[attachment_id])
            return

        async def go() -> bytes:
            data = await self.client.attachment(attachment_id)
            self.attachment_cache[attachment_id] = data
            return data

        self.call(go(), on_done)

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
        runner.on_clis = self.clis_changed.emit  # called on the runner thread; Qt queues it to the GUI
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

    def redetect_clis(self) -> bool:
        """Look for Claude Code / Cursor / Copilot again (e.g. right after installing one); False if the runner is off."""
        runner, loop = self.runner, self.runner_loop
        if runner is None or loop is None or loop.is_closed():
            return False
        asyncio.run_coroutine_threadsafe(runner.redetect(), loop)
        return True

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
        # While Qt is quitting, qasync may no longer count as a running asyncio loop, so asyncio.to_thread() and
        # httpx's aclose() can raise "no running event loop". Stop the runner thread directly (it joins with a
        # timeout) and close the client only if the loop still runs; the process is exiting anyway.
        self.stop_runner()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            await self.client.close()
        except RuntimeError:
            pass
