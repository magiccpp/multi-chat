"""Right-hand chat panel: model tabs, message cards with votes, approvals and the composer."""

from __future__ import annotations

import html
import re
from typing import Any

from PySide6.QtCore import QMimeData, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from multichat_client.state import EFFORT_LABELS, EFFORTS, MessageView, window_label
from multichat_desktop import attachments as att_mod
from multichat_desktop.attachments import AttachmentView, PendingAttachment, PendingChip
from multichat_desktop.controller import Controller
from multichat_desktop.theme import colors


BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def money(v: float) -> str:
    if not v:
        return "$0"
    return f"${v:.4f}" if v < 1 else f"${v:.2f}"


def tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


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
        self.attachments: AttachmentView | None = None
        if self.view.attachments:
            self.attachments = AttachmentView(ctl, self.view.attachments)
            layout.addWidget(self.attachments)

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
        if m.role == "summary":
            title = (f'<span style="color:{c["muted"]}">#{m.seq} 🗜</span> <b style="color:{color}">{name}</b> '
                     f'<span style="color:{c["muted"]}">context compacted</span>')
        if m.role == "user" and m.recipients and len(m.recipients) < len(st.participants):
            title += f' <span style="color:{c["muted"]}">→ {html.escape(", ".join(st.name(r) for r in m.recipients))}</span>'
        if m.phase == "build" and is_reply:
            title += ' <span style="background:#7c3aed;color:white">&nbsp;BUILD&nbsp;</span>'
        if m.status == "streaming":
            title += f' <span style="color:{c["accent"]}">…</span>'
        elif m.status in ("error", "cancelled"):
            title += f' <span style="color:{c["delfg"]}">[{m.status}]</span>'
        if is_reply and m.usage:
            u = m.usage
            approx = "~" if u.get("estimated") else ""
            title += (f' &nbsp;<span style="color:{c["muted"]}" title="{u["input_tokens"]} in / {u["output_tokens"]} out">'
                      f'{approx}{tokens(u["input_tokens"] + u["output_tokens"])} tok · {money(u["cost"])}</span>')
        self.title.setText(title)

        for w in (self.up, self.down, self.copy):
            w.setVisible(is_reply)
        self.builder.setVisible(is_reply and st.session.get("phase") == "plan" and m.status == "complete")
        self.up.setChecked(m.vote == 1)
        self.down.setChecked(m.vote == -1)
        self.setStyleSheet(f"QFrame#card {{ border-left: 4px solid {color if is_reply else c['border']}; }}")

        text = m.content or ("_thinking…_" if m.status == "streaming" and not m.tool_calls else "")
        if is_reply:
            text = BR.sub(" · ", text)  # models put <br> in table cells; Qt's Markdown drops raw HTML
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


def price_unknown(st, model_id: int | None) -> bool:  # noqa: ANN001
    """No price configured or found for this model, so its cost reads $0 although tokens were used."""
    m = st.models.get(model_id or -1)
    u = st.usage.get(model_id)
    return bool(m and u and not u.cost and not (m.get("input_price") or m.get("output_price"))
                and m.get("endpoint_kind") not in ("mock",) and u.input_tokens + u.output_tokens > 0)


class CostBar(QFrame):
    """What this session has cost so far, per model and in total. Click a model to open its tab."""

    model_clicked = Signal(object)
    details_clicked = Signal()
    context_clicked = Signal()

    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.setObjectName("costbar")
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(8, 4, 8, 4)
        self.row.setSpacing(6)
        self.refresh()

    def refresh(self) -> None:
        while self.row.count():
            item = self.row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        st = self.ctl.st
        title = QLabel("<b>Session cost</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        self.row.addWidget(title)
        ids = [p["model_id"] for p in st.participants]
        ids += [mid for mid in st.usage if mid is not None and mid not in ids]  # models that left keep their cost
        for mid in sorted(ids, key=lambda m: -(st.usage[m].cost if m in st.usage else 0)):
            u = st.usage.get(mid)
            chip = QToolButton()
            chip.setObjectName("costchip")
            unknown = price_unknown(st, mid)
            spent = "price unknown" if unknown else (money(u.cost) if u else "$0")
            tok = f" · {'~' if u and u.estimated else ''}{tokens(u.input_tokens + u.output_tokens)} tok" if u else ""
            chip.setText(f"● {st.name(mid)}  {spent}{tok}")
            chip.setStyleSheet(f"QToolButton#costchip {{ color: {st.color(mid)}; }}")
            tip = f"{st.name(mid)}: no usage yet"
            if u:
                tip = (f"{st.name(mid)}: {u.input_tokens:,} input + {u.output_tokens:,} output tokens, {money(u.cost)}"
                       + (" (token counts estimated)" if u.estimated else ""))
            if unknown:
                tip += "\nNo price is known for this model: set its prices in Endpoints (cost per 1M tokens)."
            chip.setToolTip(tip + "\nClick to show only this model's replies.")
            chip.clicked.connect(lambda _=False, mid=mid: self.model_clicked.emit(mid))
            self.row.addWidget(chip)
        self.row.addStretch(1)
        total = QLabel(f"<b>Σ {money(st.total_cost())}</b>")
        total.setTextFormat(Qt.TextFormat.RichText)
        total.setToolTip("Total for this session, all models")
        self.row.addWidget(total)
        context = QToolButton()
        context.setObjectName("costchip")
        context.setText("Context…")
        context.setToolTip("How full each model's context window is; compact now")
        context.clicked.connect(self.context_clicked.emit)
        self.row.addWidget(context)
        details = QToolButton()
        details.setObjectName("costchip")
        details.setText("Details…")
        details.setToolTip("Cost per model: replies, input and output tokens, prices, input and output cost, share")
        details.clicked.connect(self.details_clicked.emit)
        self.row.addWidget(details)


class CostDetailsDialog(QDialog):
    """Per-model cost breakdown of the session (from the server, so it includes models that left)."""

    COLS = ["Model", "Replies", "Input tokens", "Output tokens", "$ / 1M in", "$ / 1M out", "Input cost", "Output cost",
            "Total", "Share"]

    def __init__(self, ctl: Controller, rows: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Session cost — {ctl.st.session.get('title', '')}")
        self.resize(980, 120 + 34 * (len(rows) + 2))
        layout = QVBoxLayout(self)
        total = sum(r["cost"] for r in rows)
        table = QTableWidget(len(rows) + 1, len(self.COLS))
        table.setHorizontalHeaderLabels(self.COLS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        def cell(r: int, c: int, text: str, color: str | None = None, bold: bool = False, tip: str = "") -> None:
            item = QTableWidgetItem(text)
            if c > 0:
                item.setTextAlignment(right)
            if color:
                item.setForeground(QColor(color))
            if bold:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            if tip:
                item.setToolTip(tip)
            table.setItem(r, c, item)

        unknown_tip = "No price known for this model; set it in Endpoints"
        for r, u in enumerate(rows):
            unknown = not u.get("price_known", True)
            approx = "~" if u["estimated"] else ""
            cell(r, 0, f"● {u['display_name']}", u["color"])
            cell(r, 1, str(u.get("replies", 0)))
            cell(r, 2, f"{approx}{u['input_tokens']:,}")
            cell(r, 3, f"{approx}{u['output_tokens']:,}")
            cell(r, 4, "?" if unknown else f"{u.get('input_price', 0):g}", tip=unknown_tip if unknown else "")
            cell(r, 5, "?" if unknown else f"{u.get('output_price', 0):g}", tip=unknown_tip if unknown else "")
            cell(r, 6, "—" if u.get("input_cost") is None else money(u["input_cost"]))
            cell(r, 7, "—" if u.get("output_cost") is None else money(u["output_cost"]))
            cell(r, 8, "price unknown" if unknown else money(u["cost"]), bold=True, tip=unknown_tip if unknown else "")
            cell(r, 9, f"{100 * u['cost'] / total:.0f}%" if total else "—")
        n = len(rows)
        cell(n, 0, "Session total", bold=True)
        cell(n, 1, str(sum(u.get("replies", 0) for u in rows)), bold=True)
        cell(n, 2, f"{sum(u['input_tokens'] for u in rows):,}", bold=True)
        cell(n, 3, f"{sum(u['output_tokens'] for u in rows):,}", bold=True)
        cell(n, 8, money(total), bold=True)
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(table)
        note = QLabel("Costs are what the vendor reported (OpenRouter) or tokens × the model's price. Models whose vendor "
                      "doesn't publish prices get OpenRouter's list price for the same model; '~' marks estimated token counts.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        self.table = table


class ContextDialog(QDialog):
    """How full each model's context window is (like Claude Code's /context), with Compact now."""

    COLS = ["Model", "Used (est.)", "Window", "Full", "Compactions", ""]

    def __init__(self, ctl: Controller, rows: list[dict], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctl = ctl
        self.setWindowTitle("Context windows")
        self.resize(760, 140 + 34 * len(rows))
        layout = QVBoxLayout(self)
        self.table = QTableWidget(len(rows), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for r, row in enumerate(rows):
            pct = row.get("percent")
            values = [row["display_name"], f"{row['used']:,}" if row.get("used") is not None else "—",
                      window_label(row.get("window")) if row.get("window") else "—",
                      f"{pct:.0f}%" if pct is not None else "—", str(row.get("compactions", 0)), row.get("note", "")]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setForeground(QColor(ctl.st.color(row["model_id"])))
                if col == 3 and pct is not None and pct >= 70:
                    item.setForeground(QColor("#d97706"))
                self.table.setItem(r, col, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        note = QLabel("Each model compacts automatically at 80%: it summarises the older conversation and keeps the "
                      "newest messages word for word.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        row.addWidget(note, 1)
        self.compact_btn = QPushButton("Compact now")
        self.compact_btn.setToolTip("Summarise every model's older history now (keeps the latest exchange)")
        self.compact_btn.clicked.connect(self._compact)
        self.compact_btn.setEnabled(not ctl.st.busy and bool(ctl.st.messages))
        row.addWidget(self.compact_btn)
        layout.addLayout(row)

    def _compact(self) -> None:
        self.compact_btn.setEnabled(False)
        self.compact_btn.setText("Compacting…")

        def done(results: list[dict]) -> None:
            n = sum(1 for r in results if r["compacted"])
            self.ctl.notice.emit(f"Compacted {n} of {len(results)} models")
            self.accept()

        self.ctl.call(self.ctl.client.compact(self.ctl.st.session_id), done)


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
    """Message box. Enter sends; pasting or dropping images/files attaches them instead of inserting text."""

    submitted = Signal()
    attached = Signal(list)  # list[PendingAttachment]
    attach_failed = Signal(str)

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)

    def canInsertFromMimeData(self, source: QMimeData) -> bool:  # noqa: N802 - Qt API
        return att_mod.has_attachments(source) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802 - Qt API (paste and drop)
        if att_mod.has_attachments(source):
            try:
                found = att_mod.from_mime(source)
            except (OSError, ValueError) as exc:
                self.attach_failed.emit(str(exc))
                return
            if found:
                self.attached.emit(found)
                return
        super().insertFromMimeData(source)


class ChatPanel(QWidget):
    PLACEHOLDER = "Message the models…  (Enter to send, Shift+Enter for a new line, Ctrl+V pastes images)"

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
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.customContextMenuRequested.connect(self._tab_menu)
        layout.addWidget(self.tabs)
        self.cost_bar = CostBar(ctl)
        self.cost_bar.model_clicked.connect(self.show_model_tab)
        self.cost_bar.details_clicked.connect(self.show_cost_details)
        self.cost_bar.context_clicked.connect(self.show_context)
        layout.addWidget(self.cost_bar)

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

        # attachments waiting to be sent with the next message
        self.pending: list[PendingAttachment] = []
        self.pending_row = QHBoxLayout()
        self.pending_row.setContentsMargins(8, 0, 8, 0)
        self.pending_widget = QWidget()
        self.pending_widget.setLayout(self.pending_row)
        self.pending_widget.setVisible(False)
        layout.addWidget(self.pending_widget)

        self.composer = Composer()
        self.composer.setObjectName("composer")
        self.composer.setPlaceholderText(self.PLACEHOLDER)
        self.composer.setFixedHeight(86)
        self.composer.submitted.connect(self._send_clicked)
        self.composer.attached.connect(self.add_attachments)
        self.composer.attach_failed.connect(ctl.error.emit)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 4, 8, 8)
        bottom.addWidget(self.composer, 1)
        buttons = QVBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self._send_clicked)
        self.attach_btn = QPushButton("📎 Attach")
        self.attach_btn.setToolTip("Attach documents or images (you can also paste images with Ctrl+V or drop files here)")
        self.attach_btn.clicked.connect(self.choose_files)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(ctl.stop)
        buttons.addWidget(self.send_btn)
        buttons.addWidget(self.attach_btn)
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

    def _tab_label(self, model_id: int | None) -> str:
        st = self.ctl.st
        if model_id is None:
            return f"All  {money(st.total_cost())}" if st.usage else "All"
        u = st.usage.get(model_id)
        if not u:
            return st.name(model_id)
        return f"{st.name(model_id)}  {'$?' if price_unknown(st, model_id) else money(u.cost)}"

    def _tab_tip(self, model_id: int | None) -> str:
        p = self.ctl.st.participant(model_id)
        if not p:
            return "All models"
        effort = EFFORT_LABELS.get(p.get("effort") or "", "default")
        window = window_label(p.get("context_window") or p.get("context_length"))
        return (f"{p['display_name']}\nContext window: {window} (fixed for this session)\nEffort: {effort}"
                + ("\nRight-click to change the effort" if p.get("supports_effort") else ""))

    def _tab_menu(self, pos) -> None:  # noqa: ANN001
        mid = self.tabs.tabData(self.tabs.tabAt(pos))
        p = self.ctl.st.participant(mid)
        if not p:
            return
        menu = QMenu(self)
        menu.addAction(f"Context window: {window_label(p.get('context_window') or p.get('context_length'))} (fixed)").setEnabled(False)
        effort_menu = menu.addMenu("Effort")
        effort_menu.setEnabled(bool(p.get("supports_effort")))
        for label, value in [("Default", None)] + [(EFFORT_LABELS[e], e) for e in EFFORTS]:
            act = effort_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(p.get("effort") == value)
            act.triggered.connect(lambda _=False, v=value: self.ctl.call(
                self.ctl.client.update_model_settings(self.ctl.st.session_id, mid, effort=v), self.ctl.set_session))
        menu.exec(self.tabs.mapToGlobal(pos))

    def update_costs(self) -> None:
        for i in range(self.tabs.count()):
            self.tabs.setTabText(i, self._tab_label(self.tabs.tabData(i)))
            self.tabs.setTabToolTip(i, self._tab_tip(self.tabs.tabData(i)))
        self.cost_bar.refresh()

    def show_context(self) -> None:
        def show(rows: list[dict]) -> None:
            self.context_dialog = ContextDialog(self.ctl, rows, self)
            self.context_dialog.open()

        self.ctl.call(self.ctl.client.context_usage(self.ctl.st.session_id), show)

    def show_cost_details(self) -> None:
        def show(rows: list[dict]) -> None:
            self.cost_dialog = CostDetailsDialog(self.ctl, rows, self)
            self.cost_dialog.open()

        self.ctl.call(self.ctl.client.usage(self.ctl.st.session_id), show)

    def show_model_tab(self, model_id: int) -> None:
        for i in range(self.tabs.count()):
            if self.tabs.tabData(i) == model_id:
                self.tabs.setCurrentIndex(i)

    def rebuild_tabs(self) -> None:
        wanted = [("All", None)] + [(p["display_name"], p["model_id"]) for p in self.ctl.st.participants]
        # rebuilt below if the models changed; tooltips (effort, context) follow every session update
        current = [self.tabs.tabData(i) for i in range(self.tabs.count())]
        if current == [mid for _, mid in wanted]:
            self.update_costs()
            return
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        for i, (_label, mid) in enumerate(wanted):
            self.tabs.addTab(self._tab_label(mid))
            self.tabs.setTabData(i, mid)
            if mid:
                self.tabs.setTabTextColor(i, self.ctl.st.color(mid))
        self.tabs.blockSignals(False)
        self.tab_model = None
        self.tabs.setCurrentIndex(0)
        self.update_costs()

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
            f"Tell {self.ctl.st.name(builder)} what to build…" if build else self.PLACEHOLDER
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

    # ---- sending & attachments ----
    def _send_clicked(self) -> None:
        text = self.composer.toPlainText().strip()
        if not text and not self.pending:
            return
        if text.startswith("#") and not self.pending:
            # like Claude Code: "# always use tabs" goes into memory instead of to the models
            self.composer.clear()
            self.ctl.call(self.ctl.client.remember(text), lambda _: self.ctl.notice.emit("Saved to memory: " + text.lstrip("# ")))
            return
        self.composer.clear()
        pending, self.pending = self.pending, []
        self._rebuild_pending()
        self.ctl.send(text, pending)

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach files", "",
            "Documents and images (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.pdf *.docx *.txt *.md *.csv *.json *.yaml *.yml "
            "*.xml *.html *.log *.py *.js *.ts *.cs *.java *.go *.rs *.c *.cpp *.h *.sql *.ps1 *.sh);;All files (*)",
        )
        found = []
        for path in paths:
            try:
                found.append(att_mod.from_path(path))
            except (OSError, ValueError) as exc:
                self.ctl.error.emit(str(exc))
        self.add_attachments(found)

    def add_attachments(self, items: list[PendingAttachment]) -> None:
        self.pending.extend(items)
        self._rebuild_pending()
        self.composer.setFocus()

    def _remove_pending(self, chip: PendingChip) -> None:
        if chip.att in self.pending:
            self.pending.remove(chip.att)
        self._rebuild_pending()

    def _rebuild_pending(self) -> None:
        while self.pending_row.count():
            item = self.pending_row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for att in self.pending:
            chip = PendingChip(att)
            chip.removed.connect(self._remove_pending)
            self.pending_row.addWidget(chip)
        self.pending_row.addStretch(1)
        self.pending_widget.setVisible(bool(self.pending))

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
        elif etype == "usage":
            if ev.get("message_id") in self.cards:
                self.dirty.add(ev["message_id"])
            self.update_costs()
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
