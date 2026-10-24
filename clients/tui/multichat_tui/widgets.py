from __future__ import annotations

from rich.console import Group
from textual import events
from textual.message import Message
from textual.widgets import Static, TextArea

from multichat_client.state import MessageView, SessionState
from multichat_tui.render import message_body, message_header


class MessageWidget(Static, can_focus=True):
    """One message in the log. Focus it (↑/↓) to vote, pick it as builder or expand tool output."""

    DEFAULT_CSS = """
    MessageWidget { padding: 0 1; margin: 0 0 1 0; }
    MessageWidget.user { border-left: thick $primary; }
    MessageWidget.assistant { border-left: thick $panel-lighten-2; }
    MessageWidget:focus { background: $boost; border-left: thick $accent; }
    """

    def __init__(self, st: SessionState, message_id: int) -> None:
        super().__init__()
        self.st = st
        self.message_id = message_id
        self.expanded = False
        self.focused_now = False  # has_focus is not yet updated inside on_blur
        view = st.messages[message_id]
        self.add_class(view.role)

    @property
    def view(self) -> MessageView:
        return self.st.messages[self.message_id]

    def refresh_content(self) -> None:
        m = self.view
        if m.role == "assistant":
            self.styles.border_left = ("thick", "white" if self.focused_now else self.st.color(m.model_id))
        hints = self.focused_now and m.role == "assistant"
        self.update(Group(message_header(self.st, m, focused=hints), message_body(self.st, m, expanded=self.expanded)))

    def on_mount(self) -> None:
        self.refresh_content()

    def on_focus(self) -> None:
        self.focused_now = True
        self.refresh_content()

    def on_blur(self) -> None:
        self.focused_now = False
        self.refresh_content()


class PromptArea(TextArea):
    """Multi-line input: Enter sends, Alt+Enter / Ctrl+J inserts a newline."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(text))
                self.load_text("")
            return
        if not self.text and event.key.isdigit() and await self.app.quickvote_key(event.key):  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()
            return
        if event.key in ("alt+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)
