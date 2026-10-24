"""Right-hand chat panel: model tabs, message cards with votes, approvals and the composer."""

from __future__ import annotations

import html
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from multichat_client.state import MessageView
from multichat_desktop.controller import Controller
from multichat_desktop.theme import colors


def args_summary(arguments: dict[str, Any]) -> str:
    for key in ("command", "path", "file_path", "url", "query", "pattern"):
        if key in arguments:
            return str(arguments[key]).replace("\n", " ")[:120]
    return ""


class MessageCard(QFrame):
    def __init__(self, ctl: Controller, message_id: int) -> None:
        super().__init__()
        self.ctl = ctl
        self.message_id = message_id
        self.expanded = False
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(self.title)
        header.addStretch(1)
        self.up = self._tool("▲", "Upvote", lambda: self._vote(1))
        self.down = self._tool("▼", "Downvote", lambda: self._vote(-1))
        self.builder = self._tool("★ Pick as builder", "Use this model to build (switches to BUILD)", self._pick, checkable=False)
        self.copy = self._tool("⧉", "Copy reply", self._copy, checkable=False)
        for w in (self.up, self.down, self.builder, self.copy):
            header.addWidget(w)
        layout.addLayout(header)

        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.MarkdownText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.body.setOpenExternalLinks(True)
        self.body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(self.body)

        self.tools = QLabel()
        self.tools.setTextFormat(Qt.TextFormat.RichText)
        self.tools.setWordWrap(True)
        self.tools.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.tools.linkActivated.connect(self._toggle)
        layout.addWidget(self.tools)
        self.refresh()

    def _tool(self, text: str, tip: str, slot, checkable: bool = True) -> QToolButton:  # noqa: ANN001
        b = QToolButton()
        b.setObjectName("vote")
        b.setText(text)
        b.setToolTip(tip)
        b.setCheckable(checkable)
        b.clicked.connect(slot)
        return b

    @property
    def view(self) -> MessageView:
        return self.ctl.st.messages[self.message_id]

    def _vote(self, value: int) -> None:
        self.ctl.vote(self.message_id, 0 if self.view.vote == value else value)

    def _pick(self) -> None:
        if self.view.model_id:
            self.ctl.pick_builder(self.view.model_id)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.view.content)

    def _toggle(self, _: str) -> None:
        self.expanded = not self.expanded
        self.refresh()

    def refresh(self) -> None:
        st, m, c = self.ctl.st, self.view, colors()
        is_reply = m.role == "assistant"
        name = "You" if m.role == "user" else html.escape(st.name(m.model_id))
        color = c["text"] if m.role == "user" else st.color(m.model_id)
        title = f'<span style="color:{c["muted"]}">#{m.seq}</span> <b style="color:{color}">{"● " if is_reply else ""}{name}</b>'
        if m.role == "user" and m.recipients and len(m.recipients) < len(st.participants):
            title += f' <span style="color:{c["muted"]}">→ {html.escape(", ".join(st.name(r) for r in m.recipients))}</span>'
        if m.phase == "build" and is_reply:
            title += ' <span style="background:#7c3aed;color:white">&nbsp;BUILD&nbsp;</span>'
        if m.status == "streaming":
            title += f' <span style="color:{c["accent"]}">…</span>'
        elif m.status in ("error", "cancelled"):
            title += f' <span style="color:{c["delfg"]}">[{m.status}]</span>'
        self.title.setText(title)

        for w in (self.up, self.down, self.copy):
            w.setVisible(is_reply)
        self.builder.setVisible(is_reply and st.session.get("phase") == "plan" and m.status == "complete")
        self.up.setChecked(m.vote == 1)
        self.down.setChecked(m.vote == -1)
        self.setStyleSheet(f"QFrame#card {{ border-left: 4px solid {color if is_reply else c['border']}; }}")

        text = m.content or ("_thinking…_" if m.status == "streaming" and not m.tool_calls else "")
        # User prompts are shown verbatim; model replies are Markdown.
        self.body.setTextFormat(Qt.TextFormat.MarkdownText if is_reply else Qt.TextFormat.PlainText)
        self.body.setText(text)
        self.body.setVisible(bool(text))
        self.tools.setText(self._tools_html())
        self.tools.setVisible(bool(m.tool_calls))

    def _tools_html(self) -> str:
        st, m, c = self.ctl.st, self.view, colors()
        rows = []
        for call in m.tool_calls:
            summary = html.escape(args_summary(call.get("arguments") or {}))
            ext = f' <span style="color:{c["muted"]}">(run by the agent CLI)</span>' if call.get("external") else ""
            rows.append(f'<div style="font-family:monospace;color:#0891b2">⚙ <b>{html.escape(call.get("name", ""))}</b> {summary}{ext}</div>')
            result = st.results.get(call.get("id", ""))
            body = result.content if result else st.progress.get(call.get("id", ""), "")
            if body:
                lines = body.rstrip().splitlines()
                shown = lines if self.expanded else lines[:6]
                pre = html.escape("\n".join(shown))
                rows.append(f'<pre style="margin:2px 0 4px 16px;color:{c["muted"]}">{pre}</pre>')
                if len(lines) > len(shown):
                    rows.append(f'<a href="more" style="margin-left:16px">show {len(lines) - len(shown)} more lines</a>')
                elif self.expanded and len(lines) > 6:
                    rows.append('<a href="less" style="margin-left:16px">show less</a>')
        return "".join(rows)


class ApprovalCard(QFrame):
    """Inline approve/reject for commands (edits also open a diff tab in the editor)."""

    def __init__(self, ctl: Controller, event: dict[str, Any]) -> None:
        super().__init__()
        self.setObjectName("approval")
        self.ctl = ctl
        self.call_id = event["call_id"]
        layout = QVBoxLayout(self)
        who = ctl.st.name(event.get("model_id"))
        kind = event.get("kind")
        if kind == "command":
            text = f"<b>{html.escape(who)}</b> wants to run:<br><code>{html.escape(event.get('command', ''))}</code>"
        elif kind == "edit":
            text = f"<b>{html.escape(who)}</b> wants to edit <code>{html.escape(event.get('path', ''))}</code> — the diff is open in the editor."
        else:
            text = f"<b>{html.escape(who)}</b> wants to use {html.escape(str(event.get('tool')))}"
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(label)
        row = QHBoxLayout()
        self.approve_btn = QPushButton("Approve")
        self.approve_btn.setObjectName("primary")
        self.reject_btn = QPushButton("Reject")
        self.approve_btn.clicked.connect(lambda: self.decide(True))
        self.reject_btn.clicked.connect(lambda: self.decide(False))
        row.addWidget(self.approve_btn)
        row.addWidget(self.reject_btn)
        row.addStretch(1)
        layout.addLayout(row)

    def decide(self, approve: bool) -> None:
        self.ctl.approve(self.call_id, approve)
        self.deleteLater()


class Composer(QPlainTextEdit):
    submitted = Signal(str)

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            text = self.toPlainText().strip()
            if text:
                self.submitted.emit(text)
                self.clear()
            return
        super().keyPressEvent(e)


class ChatPanel(QWidget):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.cards: dict[int, MessageCard] = {}
        self.approval_cards: dict[str, ApprovalCard] = {}
        self.dirty: set[int] = set()
        self.tab_model: int | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.list = QVBoxLayout(inner)
        self.list.setContentsMargins(8, 8, 8, 8)
        self.list.setSpacing(8)
        self.list.addStretch(1)
        self.scroll.setWidget(inner)
        layout.addWidget(self.scroll, 1)

        self.chips_row = QHBoxLayout()
        self.chips_row.setContentsMargins(8, 0, 8, 0)
        chips_widget = QWidget()
        chips_widget.setLayout(self.chips_row)
        layout.addWidget(chips_widget)
        self.chips: dict[int, QPushButton] = {}

        self.composer = Composer()
        self.composer.setObjectName("composer")
        self.composer.setPlaceholderText("Message the models…  (Enter to send, Shift+Enter for a new line)")
        self.composer.setFixedHeight(86)
        self.composer.submitted.connect(self._send)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 4, 8, 8)
        bottom.addWidget(self.composer, 1)
        buttons = QVBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self._send_clicked)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(ctl.stop)
        buttons.addWidget(self.send_btn)
        buttons.addWidget(self.stop_btn)
        bottom.addLayout(buttons)
        layout.addLayout(bottom)

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._flush)
        self.timer.start()

    # ---- building ----
    def rebuild(self) -> None:
        for card in list(self.cards.values()) + list(self.approval_cards.values()):
            card.deleteLater()
        self.cards.clear()
        self.approval_cards.clear()
        for m in self.ctl.st.ordered():
            if m.role != "tool":
                self._add_card(m.id)
        self.rebuild_tabs()
        self.rebuild_chips()
        self.update_controls()
        QTimer.singleShot(50, self._scroll_end)

    def rebuild_tabs(self) -> None:
        wanted = [("All", None)] + [(p["display_name"], p["model_id"]) for p in self.ctl.st.participants]
        current = [(self.tabs.tabText(i), self.tabs.tabData(i)) for i in range(self.tabs.count())]
        if current == wanted:
            return
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        for i, (label, mid) in enumerate(wanted):
            self.tabs.addTab(label)
            self.tabs.setTabData(i, mid)
            if mid:
                self.tabs.setTabTextColor(i, self.ctl.st.color(mid))
        self.tabs.blockSignals(False)
        self.tab_model = None
        self.tabs.setCurrentIndex(0)

    def rebuild_chips(self) -> None:
        while self.chips_row.count():
            item = self.chips_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.chips.clear()
        label = QLabel("To:")
        label.setObjectName("muted")
        self.chips_row.addWidget(label)
        for p in self.ctl.st.participants:
            chip = QPushButton(p["display_name"])
            chip.setObjectName("chip")
            chip.setCheckable(True)
            chip.setChecked(self.ctl.recipients is None or p["model_id"] in self.ctl.recipients)
            chip.toggled.connect(self._recipients_changed)
            self.chips[p["model_id"]] = chip
            self.chips_row.addWidget(chip)
        self.chips_row.addStretch(1)

    def _recipients_changed(self) -> None:
        chosen = [mid for mid, chip in self.chips.items() if chip.isChecked()]
        self.ctl.recipients = None if len(chosen) == len(self.chips) else chosen

    def update_controls(self) -> None:
        build = self.ctl.st.session.get("phase") == "build"
        self.chips_row.parentWidget().setVisible(not build)  # BUILD talks to the builder only
        self.stop_btn.setEnabled(self.ctl.st.busy)
        builder = self.ctl.st.session.get("builder_model_id")
        self.composer.setPlaceholderText(
            f"Tell {self.ctl.st.name(builder)} what to build…" if build else "Message the models…  (Enter to send, Shift+Enter for a new line)"
        )

    def _add_card(self, message_id: int) -> None:
        if message_id in self.cards:
            return
        card = MessageCard(self.ctl, message_id)
        card.setVisible(self._visible(message_id))
        self.cards[message_id] = card
        self.list.insertWidget(self.list.count() - 1, card)

    def _visible(self, message_id: int) -> bool:
        return self.tab_model is None or any(m.id == message_id for m in self.ctl.st.ordered(self.tab_model))

    def _tab_changed(self, index: int) -> None:
        self.tab_model = self.tabs.tabData(index) if index >= 0 else None
        for mid, card in self.cards.items():
            card.setVisible(self._visible(mid))

    def _scroll_end(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _flush(self) -> None:
        if not self.dirty:
            return
        bar = self.scroll.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 40
        for mid in self.dirty:
            if mid in self.cards:
                self.cards[mid].refresh()
        self.dirty.clear()
        if at_end:
            QTimer.singleShot(0, self._scroll_end)

    def _send_clicked(self) -> None:
        text = self.composer.toPlainText().strip()
        self.composer.clear()
        self._send(text)

    def _send(self, text: str) -> None:
        if text:
            self.ctl.send(text)

    # ---- events ----
    def _owner(self, call_id: str) -> int | None:
        for m in self.ctl.st.messages.values():
            if any(c.get("id") == call_id for c in m.tool_calls):
                return m.id
        return None

    def on_event(self, ev: dict[str, Any]) -> None:
        etype = ev.get("type")
        if etype in ("message", "message_start", "message_end"):
            m = ev["message"]
            if m["role"] != "tool":
                self._add_card(m["id"])
                self.dirty.add(m["id"])
        elif etype in ("delta", "tool_call"):
            self.dirty.add(ev["message_id"])
        elif etype in ("tool_progress", "tool_result"):
            owner = self._owner(ev.get("call_id") or "")
            if owner:
                self.dirty.add(owner)
        elif etype == "vote":
            self.dirty.add(ev["message_id"])
        elif etype == "approval_required":
            card = ApprovalCard(self.ctl, ev)
            self.approval_cards[ev["call_id"]] = card
            self.list.insertWidget(self.list.count() - 1, card)
            QTimer.singleShot(50, self._scroll_end)
        elif etype == "approval_resolved":
            card = self.approval_cards.pop(ev["call_id"], None)
            if card:
                card.deleteLater()
        elif etype == "session":
            self.rebuild_tabs()
            self.rebuild_chips()
            self.update_controls()
            self.dirty.update(self.cards)  # "pick as builder" visibility depends on the phase
        elif etype in ("turn_start", "turn_end"):
            self.update_controls()
            if etype == "turn_end":
                self.dirty.update(self.cards)


def monospace() -> QFont:
    font = QFont("Cascadia Mono")
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font
