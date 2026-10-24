"""The MultiChat terminal UI."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static, Tab, Tabs

from multichat_client.client import ApiError, MultiChatClient
from multichat_client.config import ClientConfig
from multichat_client.runner.agent import Runner
from multichat_client.state import EFFORTS, SessionState, context_choices, window_label
from multichat_tui import commands as cmds
from multichat_tui.render import context_table, cost_table, money, tokens, usage_text
from multichat_tui.screens import (
    ApprovalScreen,
    ModelsScreen,
    SessionsScreen,
    TextScreen,
    endpoints_body,
    help_body,
    stats_body,
)
from multichat_tui.widgets import MessageWidget, PromptArea


class SlashCommands(Provider):
    """Exposes the slash commands in the ctrl+p palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for c in cmds.COMMANDS:
            score = matcher.match(f"/{c.name} {c.help}")
            if score > 0:
                yield Hit(score, matcher.highlight(f"/{c.name}  {c.help}"), self._run(c), help=c.usage)

    def _run(self, c: cmds.Command):  # noqa: ANN202
        async def run() -> None:
            app = self.app
            assert isinstance(app, MultiChatApp)
            if c.usage == f"/{c.name}":
                await app.run_command(c.name, [])
            else:
                prompt = app.query_one(PromptArea)
                prompt.load_text(f"/{c.name} ")
                prompt.focus()
                prompt.move_cursor(prompt.document.end)

        return run


class MultiChatApp(App):
    TITLE = "MultiChat"
    CSS = """
    Screen { layout: vertical; }
    #header { height: 1; background: $primary-background; color: $text; padding: 0 1; }
    #tabs { height: 2; }
    #main { height: 1fr; }
    #log { height: 1fr; padding: 0 1; }
    #sidebar { width: 36; border-left: solid $panel-lighten-2; padding: 0 1; display: none; }
    #sidebar.visible { display: block; }
    #quickvote { height: auto; padding: 0 1; background: $boost; display: none; }
    #quickvote.visible { display: block; }
    #suggest { height: auto; max-height: 8; padding: 0 2; color: $text-muted; display: none; }
    #suggest.visible { display: block; }
    PromptArea { height: auto; min-height: 3; max-height: 10; border: round $accent; }
    #status { height: 1; padding: 0 1; background: $panel; }
    """
    COMMANDS = App.COMMANDS | {SlashCommands}
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("tab", "next_tab", "Next model tab", priority=True),
        Binding("shift+tab", "prev_tab", "Prev tab", priority=True),
        Binding("escape", "focus_prompt", "Input", show=False),
        Binding("alt+up", "focus_messages(-1)", "Replies", show=False, priority=True),
        Binding("alt+down", "focus_messages(1)", show=False, priority=True),
        Binding("up", "move_focus(-1)", show=False),
        Binding("down", "move_focus(1)", show=False),
        Binding("plus,equals_sign", "vote(1)", "Upvote", show=False),
        Binding("minus", "vote(-1)", "Downvote", show=False),
        Binding("0", "vote(0)", "Clear vote", show=False),
        Binding("b", "pick_builder", "Builder", show=False),
        Binding("y", "copy_message", "Copy", show=False),
        Binding("enter", "toggle_expand", "Expand", show=False),
        Binding("ctrl+a", "show_approval", "Approvals", show=False),
    ]

    def __init__(
        self,
        client: MultiChatClient,
        config: ClientConfig,
        *,
        session_id: int | None = None,
        start_runner: bool = True,
        runner_roots: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.config = config
        self.initial_session = session_id
        self.start_runner = start_runner
        self.runner_roots = runner_roots or config.runner_roots or [os.getcwd()]
        self.st = SessionState()
        self.models: list[dict] = []
        self.widgets: dict[int, MessageWidget] = {}
        self.dirty: set[int] = set()
        self.tab_model: int | None = None
        self.recipients: list[int] | None = None
        self.pending_files: list[Path] = []  # /attach — sent with the next message
        self.no_folder: set[int] = set()  # sessions where /folder none was chosen
        self.quickvote_enabled = True
        self.quickvote_ids: list[int] = []
        self.runner: Runner | None = None
        self.runner_state = "off"
        self._events_task: asyncio.Task | None = None
        self._approval_open = False

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield Tabs(id="tabs")
        with Horizontal(id="main"):
            yield VerticalScroll(id="log")
            with Vertical(id="sidebar"):
                yield Static(id="side-participants")
                yield Static(id="side-files")
        yield Static(id="quickvote")
        yield Static(id="suggest")
        yield PromptArea(id="prompt", placeholder="Message the models…  (/ for commands, enter to send)")
        yield Static(id="status")

    async def on_mount(self) -> None:
        self.set_interval(0.05, self._flush)
        self.query_one(PromptArea).focus()
        try:
            self.models = await self.client.models()
        except ApiError as exc:
            self.notify(f"cannot reach the server: {exc}", severity="error", timeout=20)
            return
        except Exception as exc:  # noqa: BLE001 - network errors
            self.notify(f"cannot reach {self.client.url}: {exc}", severity="error", timeout=20)
            return
        if self.start_runner:
            self.run_runner()
        sid = self.initial_session
        if sid is None:
            sessions = await self.client.sessions(limit=1)
            if sessions:
                sid = sessions[0]["id"]
            else:
                sid = (await self.client.create_session("New session"))["id"]
        await self.open_session(sid)
        if not self.st.participants:
            self.notify("Pick the models for this session (space to tick, enter to save).")
            await self.run_command("models", [])

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)

    # ------------------------------------------------------------------ runner

    @work(exclusive=True, group="runner")
    async def run_runner(self) -> None:
        def on_status(state: str, detail: str) -> None:
            self.runner_state = state
            self.call_later(self.update_header)
            if state == "online":
                self.call_later(self.attach_this_machine)
            if state == "error":
                self.notify(f"runner: {detail}", severity="error")

        self.runner = Runner(
            self.client.url,
            self.client.token,
            self.runner_roots,
            name=self.config.runner_name or None,
            on_status=on_status,
        )
        await self.runner.run_forever()

    async def on_unmount(self) -> None:
        if self.runner:
            await self.runner.stop()
        if self._events_task:
            self._events_task.cancel()

    # ------------------------------------------------------------------ session

    async def open_session(self, session_id: int) -> None:
        if self._events_task:
            self._events_task.cancel()
        session, messages, usage = await asyncio.gather(
            self.client.session(session_id), self.client.messages(session_id), self.client.usage(session_id)
        )
        self.models = await self.client.models()
        self.st = SessionState()
        self.st.load(session, messages, usage, self.models)
        try:
            self.st.file_changes = [
                {"path": c["path"], "turn_id": c["turn_id"], "reverted": c["reverted"]} for c in await self.client.changes(session_id)
            ]
        except ApiError:
            pass
        self.recipients = None
        self.tab_model = None
        await self.rebuild_log()
        self.rebuild_tabs()
        self.update_header()
        self.update_status()
        self.update_sidebar()
        self._events_task = asyncio.create_task(self._follow(session_id))
        await self.attach_this_machine()

    async def attach_this_machine(self) -> None:
        """A session without a folder uses this machine (the runner's first folder) so models can inspect and work on
        it; `/folder none` opts the session out."""
        s, r = self.st.session, self.runner
        if (not s or s.get("workspace_kind") != "none" or r is None or not r.runner_id or self.runner_state != "online"
                or s["id"] in self.no_folder or not r.roots.roots):
            return
        try:
            self.st.session = await self.client.set_workspace(s["id"], "runner", str(r.roots.roots[0]), runner_id=r.runner_id)
        except ApiError as exc:
            self.notify(f"could not attach this machine: {exc.detail}", severity="warning")
            return
        self.update_header()

    async def _follow(self, session_id: int) -> None:
        while True:
            try:
                async for event in self.client.events(session_id):
                    await self.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect on network hiccups
                await asyncio.sleep(2)

    async def rebuild_log(self) -> None:
        log = self.query_one("#log", VerticalScroll)
        await log.remove_children()
        self.widgets = {}
        for m in self.st.ordered():
            if m.role != "tool":
                await self._mount_message(m.id, scroll=False)
        log.scroll_end(animate=False)

    async def _mount_message(self, message_id: int, scroll: bool = True) -> None:
        if message_id in self.widgets:
            return
        w = MessageWidget(self.st, message_id)
        self.widgets[message_id] = w
        w.display = self._visible(message_id)
        log = self.query_one("#log", VerticalScroll)
        at_end = log.scroll_offset.y >= log.max_scroll_y - 2
        await log.mount(w)
        if scroll and at_end:
            log.scroll_end(animate=False)

    def _visible(self, message_id: int) -> bool:
        if self.tab_model is None:
            return True
        return any(m.id == message_id for m in self.st.ordered(self.tab_model))

    def _owner_of_call(self, call_id: str) -> int | None:
        for m in self.st.messages.values():
            if any(c.get("id") == call_id for c in m.tool_calls):
                return m.id
        return None

    def _flush(self) -> None:
        if not self.dirty:
            return
        log = self.query_one("#log", VerticalScroll)
        at_end = log.scroll_offset.y >= log.max_scroll_y - 2
        for mid in list(self.dirty):
            w = self.widgets.get(mid)
            if w is not None:
                w.refresh_content()
        self.dirty.clear()
        if at_end:
            log.scroll_end(animate=False)

    # ------------------------------------------------------------------ events

    async def handle_event(self, ev: dict[str, Any]) -> None:
        etype = self.st.apply(ev)
        if etype in ("message", "message_start", "message_end"):
            m = ev["message"]
            if m["role"] != "tool":
                await self._mount_message(m["id"])
                self.dirty.add(m["id"])
        elif etype == "delta":
            self.dirty.add(ev["message_id"])
        elif etype in ("tool_call",):
            self.dirty.add(ev["message_id"])
        elif etype in ("tool_progress", "tool_result"):
            owner = self._owner_of_call(ev.get("call_id") or ev.get("message", {}).get("tool_call_id", ""))
            if owner:
                self.dirty.add(owner)
        elif etype == "usage":
            self.update_status()
            self.update_sidebar()
        elif etype == "vote":
            self.dirty.add(ev["message_id"])
        elif etype == "approval_required":
            self.show_next_approval()
        elif etype == "session":
            self.rebuild_tabs()
            self.update_header()
            self.update_sidebar()
        elif etype == "file_changed":
            self.update_sidebar()
        elif etype in ("error", "notice"):
            name = self.st.name(ev.get("model_id")) if ev.get("model_id") else "server"
            self.notify(f"{name}: {ev.get('message')}", severity="error" if etype == "error" else "warning", timeout=8)
        elif etype == "turn_start":
            self.hide_quickvote()
            self.update_header()
        elif etype == "turn_end":
            self.update_header()
            self.update_status()
            self.dirty.update(m.id for m in self.st.messages.values() if m.turn_id == ev.get("turn_id"))
            self.offer_quickvote(ev.get("turn_id"))

    # ------------------------------------------------------------------ header / status / sidebar

    def update_header(self) -> None:
        s = self.st.session
        if not s:
            return
        t = Text()
        t.append(f" {s.get('title', '')} ", style="bold")
        t.append(f"· {s.get('workspace_label', '')} ")
        phase = s.get("phase", "plan").upper()
        t.append(f" {phase} ", style="bold black on yellow" if phase == "PLAN" else "bold white on magenta")
        t.append(f" {s.get('context_mode', '')} ")
        if s.get("builder_model_id"):
            t.append("· builder ")
            t.append(self.st.name(s["builder_model_id"]), style=f"bold {self.st.color(s['builder_model_id'])}")
        if self.recipients:
            t.append(" · to " + ", ".join(self.st.name(r) for r in self.recipients), style="cyan")
        t.append(f" · approvals: edits {s.get('approve_edits')}, cmds {s.get('approve_commands')}", style="dim")
        runner_style = {"online": "green", "connecting": "yellow", "offline": "red", "error": "red"}.get(self.runner_state, "dim")
        t.append(f" · runner {self.runner_state}", style=runner_style)
        if self.st.busy:
            t.append("  ⟳ working", style="bold yellow")
        self.query_one("#header", Static).update(t)

    def update_status(self) -> None:
        t = usage_text(self.st)
        if self.pending_files:
            t.append("   📎 " + ", ".join(p.name for p in self.pending_files) + " (/attach clear)", style="bold cyan")
        if self.st.approvals:
            t.append(f"   {len(self.st.approvals)} approval pending (ctrl+a)", style="bold red")
        t.append("   tab models · alt+↑ replies · ctrl+p commands", style="dim")
        self.query_one("#status", Static).update(t)

    def update_sidebar(self) -> None:
        t = Text("Participants\n", style="bold")
        for p in self.st.participants:
            u = self.st.usage.get(p["model_id"])
            t.append("● ", style=p["color"])
            t.append(p["display_name"])
            extras = [window_label(p.get("context_window") or p.get("context_length"))]
            if p.get("effort"):
                extras.append(f"effort {p['effort']}")
            t.append(f"  {' · '.join(x for x in extras if x != 'default')}", style="dim")
            if self.st.session.get("builder_model_id") == p["model_id"]:
                t.append(" ★", style="yellow")
            if u:
                t.append(f"\n   {tokens(u.input_tokens)} in · {tokens(u.output_tokens)} out · {money(u.cost)}", style="dim")
            t.append("\n")
        self.query_one("#side-participants", Static).update(t)
        f = Text("\nFiles changed\n", style="bold")
        seen: dict[str, bool] = {}
        for c in self.st.file_changes:
            seen[c["path"]] = bool(c.get("reverted"))
        for path, reverted in seen.items():
            f.append(("↺ " if reverted else "✎ ") + path + "\n", style="dim" if reverted else "")
        if not seen:
            f.append("none yet\n", style="dim")
        self.query_one("#side-files", Static).update(f)

    def rebuild_tabs(self) -> None:
        tabs = self.query_one(Tabs)
        wanted = [("tab-all", "All")] + [(f"tab-{p['model_id']}", p["display_name"]) for p in self.st.participants]
        current = [(t.id, str(t.label)) for t in tabs.query(Tab)]
        if current == wanted:
            return
        tabs.clear()
        for tab_id, label in wanted:
            tabs.add_tab(Tab(label, id=tab_id))
        active = f"tab-{self.tab_model}" if self.tab_model else "tab-all"
        if active in [w[0] for w in wanted]:
            tabs.active = active

    @on(Tabs.TabActivated)
    def tab_changed(self, event: Tabs.TabActivated) -> None:
        tab_id = event.tab.id or "tab-all"
        self.tab_model = None if tab_id == "tab-all" else int(tab_id.removeprefix("tab-"))
        for mid, w in self.widgets.items():
            w.display = self._visible(mid)
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    def action_next_tab(self) -> None:
        self.query_one(Tabs).action_next_tab()

    def action_prev_tab(self) -> None:
        self.query_one(Tabs).action_previous_tab()

    def action_toggle_sidebar(self) -> None:
        self.query_one("#sidebar").toggle_class("visible")

    # ------------------------------------------------------------------ message focus & actions

    def _visible_widgets(self) -> list[MessageWidget]:
        return [w for w in self.widgets.values() if w.display]

    def _focused_message(self) -> MessageWidget | None:
        return self.focused if isinstance(self.focused, MessageWidget) else None

    def action_focus_prompt(self) -> None:
        if isinstance(self.focused, PromptArea):
            self.hide_quickvote()
        self.query_one(PromptArea).focus()

    def action_focus_messages(self, direction: int) -> None:
        ws = self._visible_widgets()
        if ws:
            target = ws[-1] if direction < 0 else ws[0]
            target.focus()
            target.scroll_visible()

    def action_move_focus(self, direction: int) -> None:
        current = self._focused_message()
        if current is None:
            return
        ws = self._visible_widgets()
        i = ws.index(current) + direction
        if i >= len(ws):
            self.action_focus_prompt()
        elif i >= 0:
            ws[i].focus()
            ws[i].scroll_visible()

    def action_toggle_expand(self) -> None:
        w = self._focused_message()
        if w:
            w.expanded = not w.expanded
            w.refresh_content()

    async def action_vote(self, value: int) -> None:
        w = self._focused_message()
        if w is None or w.view.role != "assistant":
            return
        await self.vote(w.message_id, value)

    async def vote(self, message_id: int, value: int) -> None:
        try:
            await self.client.vote(message_id, value)
        except ApiError as exc:
            self.notify(str(exc.detail), severity="error")
            return
        m = self.st.messages.get(message_id)
        if m:
            m.vote = value
            self.dirty.add(message_id)
            label = {1: "▲ upvoted", -1: "▼ downvoted", 0: "vote cleared"}[value]
            self.notify(f"{label} #{m.seq} ({self.st.name(m.model_id)})", timeout=2)

    async def action_pick_builder(self) -> None:
        w = self._focused_message()
        if w and w.view.model_id:
            await self.pick_builder(w.view.model_id)

    async def pick_builder(self, model_id: int) -> None:
        try:
            self.st.session = await self.client.pick_builder(self.st.session_id, model_id)
        except ApiError as exc:
            self.notify(exc.detail, severity="error")
            return
        self.update_header()
        self.update_sidebar()
        self.notify(f"★ {self.st.name(model_id)} is the builder — BUILD mode")

    def action_copy_message(self) -> None:
        w = self._focused_message()
        if w:
            self.copy_to_clipboard(w.view.content)
            self.notify("copied", timeout=1)

    # ------------------------------------------------------------------ quick vote

    def offer_quickvote(self, turn_id: str | None) -> None:
        if not self.quickvote_enabled or self.st.session.get("phase") != "plan":
            return
        replies = [
            m for m in self.st.ordered() if m.turn_id == turn_id and m.role == "assistant" and m.content.strip()
        ]
        latest: dict[int, int] = {}
        for m in replies:
            latest[m.model_id or 0] = m.id
        if len(latest) < 2:
            return
        self.quickvote_ids = list(latest.values())
        t = Text("Quick vote (empty input + number): ", style="bold")
        for i, mid in enumerate(self.quickvote_ids, 1):
            m = self.st.messages[mid]
            t.append(f" {i} ", style="bold reverse")
            t.append(f" {self.st.name(m.model_id)}  ", style=self.st.color(m.model_id))
        t.append("  esc dismiss", style="dim")
        bar = self.query_one("#quickvote", Static)
        bar.update(t)
        bar.add_class("visible")

    def hide_quickvote(self) -> None:
        self.quickvote_ids = []
        self.query_one("#quickvote", Static).remove_class("visible")

    async def quickvote_key(self, key: str) -> bool:
        """Called by the prompt for digit keys while it is empty."""
        if not self.quickvote_ids or not key.isdigit() or key == "0":
            return False
        i = int(key) - 1
        if i >= len(self.quickvote_ids):
            return False
        await self.vote(self.quickvote_ids[i], 1)
        self.hide_quickvote()
        return True

    # ------------------------------------------------------------------ approvals

    def show_next_approval(self) -> None:
        self.update_status()
        if self._approval_open or not self.st.approvals:
            return
        call_id, ev = next(iter(self.st.approvals.items()))
        self._approval_open = True

        async def decided(result: tuple[bool, str | None] | None) -> None:
            self._approval_open = False
            if result is not None:
                try:
                    await self.client.approve(self.st.session_id, call_id, result[0], result[1])
                except ApiError as exc:
                    self.notify(exc.detail, severity="error")
                self.st.approvals.pop(call_id, None)
            self.update_status()
            if result is not None:
                self.show_next_approval()

        self.push_screen(ApprovalScreen(ev, self.st.name(ev.get("model_id"))), decided)

    def action_show_approval(self) -> None:
        self.show_next_approval()

    # ------------------------------------------------------------------ input

    @on(PromptArea.Changed)
    def prompt_changed(self, event: PromptArea.Changed) -> None:
        text = event.text_area.text
        box = self.query_one("#suggest", Static)
        if text.startswith("/") and " " not in text:
            hits = cmds.complete(text)
            if hits:
                body = Text()
                for c in hits[:8]:
                    body.append(f"{c.usage:<44}", style="bold cyan")
                    body.append(c.help + "\n", style="dim")
                box.update(body)
                box.add_class("visible")
                return
        box.remove_class("visible")

    @on(PromptArea.Submitted)
    async def submitted(self, event: PromptArea.Submitted) -> None:
        self.query_one("#suggest", Static).remove_class("visible")
        if event.text.startswith("#") and not event.text.startswith("##"):
            # like Claude Code: "# always use tabs" goes into memory instead of to the models
            try:
                await self.client.remember(event.text)
                self.notify("Saved to memory: " + event.text.lstrip("# "))
            except ApiError as exc:
                self.notify(exc.detail, severity="error")
            return
        parsed = cmds.parse(event.text)
        if parsed:
            await self.run_command(*parsed)
            return
        text = event.text[1:] if event.text.startswith("//") else event.text
        self.hide_quickvote()
        self.send(text)

    @work(group="chat")
    async def send(self, text: str) -> None:
        files, self.pending_files = self.pending_files, []
        self.update_status()
        sid = self.st.session_id
        try:
            ids = []
            for path in files:
                ids.append((await self.client.upload_attachment(sid, path.name, path.read_bytes()))["id"])
            async for _ in self.client.chat(sid, text, self.recipients, ids):
                pass  # events arrive through the session subscription
        except ApiError as exc:
            self.notify(exc.detail, severity="error", timeout=8)
        except OSError as exc:
            self.notify(f"cannot read attachment: {exc}", severity="error", timeout=8)

    def attach(self, args: list[str]) -> None:
        if args == ["clear"]:
            self.pending_files = []
        elif not args:
            self.notify("usage: /attach <file> [file…]   (documents: PDF, Word, text, code; images: PNG, JPEG, …)")
        for arg in args if args != ["clear"] else []:
            path = Path(arg).expanduser()
            if not path.is_file():
                self.notify(f"no such file: {arg}", severity="error")
            elif path.stat().st_size > 20 * 1024 * 1024:
                self.notify(f"{path.name} is larger than 20 MB", severity="error")
            else:
                self.pending_files.append(path)
        self.update_status()

    # ------------------------------------------------------------------ commands

    def _model(self, name: str, participants_only: bool = True) -> dict | None:
        pool = self.models
        if participants_only:
            ids = {p["model_id"] for p in self.st.participants}
            pool = [m for m in self.models if m["id"] in ids]
        m = cmds.match_model(name, pool)
        if m is None:
            self.notify(f"no {'participant' if participants_only else 'model'} matches {name!r}", severity="error")
        return m

    async def run_command(self, name: str, args: list[str]) -> None:  # noqa: C901 - a flat dispatcher reads best
        sid = self.st.session_id
        c = self.client
        try:
            if name in ("quit", "exit", "q"):
                self.exit()
            elif name == "help":
                self.push_screen(TextScreen("MultiChat — keys and commands", help_body()))
            elif name == "new" or name == "clear":
                if self.st.is_empty:
                    self.notify("This session is still empty — type a message to start it.")
                    return
                self.models = await c.models()
                available = {m["id"] for m in self.models if m["available"] and m["enabled"]}
                ids = [p["model_id"] for p in self.st.participants if p["model_id"] in available]
                s = await c.create_session(" ".join(args) or "New session", ids, self.st.session.get("context_mode", "isolated"),
                                           reuse_empty=True)
                await self.open_session(s["id"])
                if not ids:
                    await self.run_command("models", [])
            elif name == "attach":
                self.attach(args)
            elif name in ("effort", "window"):
                await self.model_setting(name, args)
            elif name == "context":
                self.push_screen(TextScreen("Context windows", context_table(await c.context_usage(sid))))
            elif name == "compact":
                self.notify("compacting…")
                results = await c.compact(sid)
                self.notify(f"compacted {sum(r['compacted'] for r in results)} of {len(results)} models")
            elif name == "memory":
                if args[:1] == ["clear"]:
                    await c.set_memory("")
                data = await c.memory()
                body = Text(data["text"] or "(empty)")
                body.append(f"\n\nAdd a line: '# text'.  Project memory: {', '.join(data['project_files'])} in the folder.", style="dim")
                self.push_screen(TextScreen("Memory (every model, every session)", body))
            elif name in ("sessions", "resume"):
                sessions = await c.sessions()

                async def chosen(session_id: int | None) -> None:
                    if session_id:
                        await self.open_session(session_id)

                self.push_screen(SessionsScreen(sessions), chosen)
            elif name == "models":
                self.models = await c.models()
                clis = set(self.runner.clis) if self.runner else set()

                async def saved(ids: list[int] | None) -> None:
                    if ids is not None:
                        self.st.session = await c.set_session_models(sid, ids)
                        self.rebuild_tabs()
                        self.update_header()
                        self.update_sidebar()

                self.push_screen(ModelsScreen(self.models, [p["model_id"] for p in self.st.participants], clis), saved)
            elif name == "plan":
                self.st.session = await c.update_session(sid, phase="plan")
            elif name in ("build", "builder"):
                if args:
                    m = self._model(" ".join(args))
                    if m:
                        if name == "builder":
                            self.st.session = await c.update_session(sid, builder_model_id=m["id"])
                        else:
                            await self.pick_builder(m["id"])
                elif name == "build":
                    self.st.session = await c.update_session(sid, phase="build")
                else:
                    self.notify("usage: /builder <model>", severity="warning")
            elif name == "mode":
                if not args or args[0] not in ("shared", "isolated", "relay"):
                    self.notify("usage: /mode isolated|shared|relay", severity="warning")
                else:
                    self.st.session = await c.update_session(sid, context_mode=args[0])
            elif name == "to":
                if not args or args[0] == "all":
                    self.recipients = None
                else:
                    found = [self._model(a) for a in ",".join(args).split(",") if a.strip()]
                    if all(found):
                        self.recipients = [m["id"] for m in found]  # type: ignore[index]
            elif name == "folder":
                await self.set_folder(args)
            elif name == "approve":
                if len(args) == 2 and args[0] in ("edits", "cmds", "commands") and args[1] in ("auto", "ask"):
                    field = "approve_edits" if args[0] == "edits" else "approve_commands"
                    self.st.session = await c.update_session(sid, **{field: args[1]})
                else:
                    self.notify("usage: /approve edits|cmds auto|ask", severity="warning")
            elif name == "vote":
                await self.vote_command(args)
            elif name == "quickvote":
                self.quickvote_enabled = not args or args[0] in ("on", "1", "yes")
                if not self.quickvote_enabled:
                    self.hide_quickvote()
                self.notify(f"quick vote {'on' if self.quickvote_enabled else 'off'}")
            elif name == "revert":
                r = await c.revert(sid)
                self.notify("reverted: " + ", ".join(r["restored"]))
                for ch in self.st.file_changes:
                    if ch.get("turn_id") == r["turn_id"]:
                        ch["reverted"] = True
                self.update_sidebar()
            elif name == "stop":
                await c.stop(sid)
            elif name == "stats":
                self.push_screen(TextScreen("Statistics — price and performance per model", stats_body(await c.stats(), compact=self.size.width < 150)))
            elif name == "cost":
                self.push_screen(TextScreen("Session cost", cost_table(await c.usage(sid))))
            elif name == "endpoints":
                if args[:1] == ["check"]:
                    self.notify("checking keys and model lists…")
                    await c.check_endpoints()
                    self.models = await c.models()
                eps, models, settings = await asyncio.gather(c.endpoints(), c.models(), c.settings())
                self.push_screen(TextScreen("Server configuration", endpoints_body(eps, models, settings)))
            elif name == "rename":
                self.st.session = await c.update_session(sid, title=" ".join(args) or "Untitled")
            else:
                self.notify(f"unknown command /{name} — try /help", severity="warning")
        except ApiError as exc:
            self.notify(exc.detail, severity="error", timeout=8)
        self.update_header()
        self.update_sidebar()

    async def model_setting(self, name: str, args: list[str]) -> None:
        if len(args) < 2:
            self.notify(cmds.BY_NAME[name].usage, severity="warning")
            return
        who, value = " ".join(args[:-1]), args[-1].lower()
        targets = self.st.participants if who == "all" else [p for p in [self._model(who)] if p]
        for p in targets:
            mid = p.get("model_id", p.get("id"))
            part = self.st.participant(mid) or {}
            if name == "effort":
                if value not in EFFORTS + ["default"]:
                    self.notify("effort: low, medium, high, xhigh, max or default", severity="warning")
                    return
                if not part.get("supports_effort"):
                    self.notify(f"{part.get('display_name', mid)} has no effort setting", severity="warning")
                    continue
                self.st.session = await self.client.update_model_settings(self.st.session_id, mid, effort=None if value == "default" else value)
            else:
                choices = context_choices(part.get("context_length"))
                wanted = {"256k": 256_000, "max": choices[-1] if choices else None}.get(value)
                if len(choices) < 2 or wanted is None:
                    self.notify(f"{part.get('display_name', mid)}: " + (
                        f"uses its maximum, {window_label(choices[0])} ({choices[0]:,} tokens); a choice exists only above 512K"
                        if choices else "context window unknown") if choices or wanted else "context: 256k or max", severity="warning")
                    continue
                self.st.session = await self.client.update_model_settings(self.st.session_id, mid, context_window=min(wanted, choices[-1]))
        self.update_sidebar()
        self.rebuild_tabs()

    async def vote_command(self, args: list[str]) -> None:
        values = {"up": 1, "+": 1, "down": -1, "-": -1, "clear": 0, "0": 0}
        if not args:
            self.notify("usage: /vote <#n> up|down|clear   or   /vote up|down", severity="warning")
            return
        if args[0] in values:  # latest reply in the current tab
            m = self.st.latest_reply(self.tab_model)
            if m is None:
                self.notify("no reply to vote on", severity="warning")
                return
            await self.vote(m.id, values[args[0]])
            return
        seq = args[0].lstrip("#")
        value = values.get(args[1] if len(args) > 1 else "up")
        m = self.st.by_seq(int(seq)) if seq.isdigit() else None
        if m is None or value is None:
            self.notify(f"no message #{seq}", severity="warning")
            return
        await self.vote(m.id, value)

    async def set_folder(self, args: list[str]) -> None:
        sid = self.st.session_id
        if not args:
            self.notify("usage: /folder none | here | <path> | ssh:<connection>:<path> | runner:<name>:<path>", severity="warning")
            return
        spec = " ".join(args)
        if spec == "none":
            self.no_folder.add(sid)
            self.st.session = await self.client.set_workspace(sid, "none")
        elif spec.startswith("ssh:"):
            _, conn_name, path = spec.split(":", 2)
            conns = {x["name"]: x for x in await self.client.ssh_connections()}
            if conn_name not in conns:
                self.notify(f"no SSH connection named {conn_name!r}", severity="error")
                return
            self.st.session = await self.client.set_workspace(sid, "ssh", path, ssh_connection_id=conns[conn_name]["id"])
        elif spec.startswith("runner:"):
            _, runner_name, path = spec.split(":", 2)
            runners = {r["name"]: r for r in await self.client.runners()}
            if runner_name not in runners:
                self.notify(f"no runner named {runner_name!r}", severity="error")
                return
            self.st.session = await self.client.set_workspace(sid, "runner", path, runner_id=runners[runner_name]["id"])
        else:
            if not self.runner or not self.runner.runner_id:
                self.notify("the local runner is not connected (start without --no-runner)", severity="error")
                return
            path = os.getcwd() if spec == "here" else str(Path(spec).expanduser().resolve())
            self.st.session = await self.client.set_workspace(sid, "runner", path, runner_id=self.runner.runner_id)
        self.notify(f"folder: {self.st.session['workspace_label']}")
