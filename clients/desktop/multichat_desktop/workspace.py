"""Left file tree and center editor area (read-only code viewer and diff views)."""

from __future__ import annotations

import html
from typing import Any

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, guess_lexer_for_filename
from pygments.util import ClassNotFound
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from multichat_desktop.controller import Controller
from multichat_desktop.theme import colors, is_dark

PATH_ROLE = Qt.ItemDataRole.UserRole
DIR_ROLE = Qt.ItemDataRole.UserRole + 1


class FileTree(QWidget):
    open_file = Signal(str)
    open_folder = Signal()

    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch(1)
        msg = QLabel("No folder attached.\nModels can still chat; builders answer with code.")
        msg.setObjectName("muted")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        btn = QPushButton("Open folder…")
        btn.setObjectName("primary")
        btn.clicked.connect(self.open_folder.emit)
        el.addWidget(msg)
        el.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)
        el.addStretch(2)
        self.stack.addWidget(empty)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.itemDoubleClicked.connect(self._activated)
        self.stack.addWidget(self.tree)

    def reload(self) -> None:
        self.tree.clear()
        if self.ctl.st.session.get("workspace_kind", "none") == "none":
            self.stack.setCurrentIndex(0)
            return
        self.stack.setCurrentIndex(1)
        self.ctl.call(self.ctl.client.tree(self.ctl.st.session_id, "."), lambda r: self._fill(self.tree.invisibleRootItem(), ".", r))

    def _fill(self, parent: QTreeWidgetItem, base: str, result: dict) -> None:
        parent.takeChildren()
        for e in result["entries"]:
            path = e["name"] if base == "." else f"{base}/{e['name']}"
            item = QTreeWidgetItem([("📁 " if e["is_dir"] else "📄 ") + e["name"]])
            item.setData(0, PATH_ROLE, path)
            item.setData(0, DIR_ROLE, e["is_dir"])
            if e["is_dir"]:
                item.addChild(QTreeWidgetItem(["…"]))  # placeholder until expanded
            parent.addChild(item)

    def _expand(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, PATH_ROLE)
        if item.childCount() == 1 and item.child(0).data(0, PATH_ROLE) is None:
            self.ctl.call(self.ctl.client.tree(self.ctl.st.session_id, path), lambda r: self._fill(item, path, r))

    def _activated(self, item: QTreeWidgetItem) -> None:
        if not item.data(0, DIR_ROLE) and item.data(0, PATH_ROLE):
            self.open_file.emit(item.data(0, PATH_ROLE))


def code_html(path: str, text: str) -> str:
    try:
        lexer = guess_lexer_for_filename(path, text)
    except ClassNotFound:
        lexer = TextLexer()
    style = "monokai" if is_dark() else "default"
    formatter = HtmlFormatter(noclasses=True, style=style, linenos="inline", nobackground=True)
    return highlight(text, lexer, formatter)


def diff_html(diff: str) -> str:
    c = colors()
    lines = []
    for line in diff.splitlines():
        esc = html.escape(line) or "&nbsp;"
        if line.startswith("+++") or line.startswith("---"):
            lines.append(f'<div style="color:{c["muted"]}">{esc}</div>')
        elif line.startswith("@@"):
            lines.append(f'<div style="color:#0891b2">{esc}</div>')
        elif line.startswith("+"):
            lines.append(f'<div style="background:{c["add"]};color:{c["addfg"]}">{esc}</div>')
        elif line.startswith("-"):
            lines.append(f'<div style="background:{c["del"]};color:{c["delfg"]}">{esc}</div>')
        else:
            lines.append(f"<div>{esc}</div>")
    return f'<div style="font-family:Cascadia Mono,Consolas,monospace;white-space:pre">{"".join(lines) or "(no changes)"}</div>'


class DiffView(QWidget):
    """A diff tab: pending edits get Accept / Reject; applied ones can revert their turn."""

    def __init__(self, ctl: Controller, path: str, diff: str, *, call_id: str | None = None, turn_id: str | None = None, title: str = "") -> None:
        super().__init__()
        self.ctl = ctl
        self.call_id = call_id
        self.turn_id = turn_id
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 6)
        self.label = QLabel(title or path)
        bar.addWidget(self.label)
        bar.addStretch(1)
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setObjectName("primary")
        self.reject_btn = QPushButton("Reject")
        self.revert_btn = QPushButton("Revert this turn")
        self.accept_btn.clicked.connect(lambda: self._decide(True))
        self.reject_btn.clicked.connect(lambda: self._decide(False))
        self.revert_btn.clicked.connect(self._revert)
        for b in (self.accept_btn, self.reject_btn, self.revert_btn):
            bar.addWidget(b)
        layout.addLayout(bar)
        self.view = QTextBrowser()
        layout.addWidget(self.view, 1)
        self.set_diff(diff)
        self._update_buttons()

    def set_diff(self, diff: str) -> None:
        self.view.setHtml(diff_html(diff))

    def _update_buttons(self) -> None:
        pending = self.call_id is not None and self.call_id in self.ctl.st.approvals
        self.accept_btn.setVisible(pending)
        self.reject_btn.setVisible(pending)
        self.revert_btn.setVisible(not pending and self.turn_id is not None)

    def _decide(self, approve: bool) -> None:
        if self.call_id:
            self.ctl.approve(self.call_id, approve)
            self.label.setText(self.label.text() + ("  — accepted" if approve else "  — rejected"))
            self.call_id = None
        self._update_buttons()

    def _revert(self) -> None:
        self.ctl.revert(self.turn_id)
        self.revert_btn.setEnabled(False)

    def approval_resolved(self, call_id: str) -> None:
        if call_id == self.call_id:
            self.call_id = None
            self._update_buttons()


class EditorTabs(QTabWidget):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.setTabsClosable(True)
        self.setDocumentMode(True)
        self.tabCloseRequested.connect(self.removeTab)
        welcome = QLabel(
            "<h2>MultiChat</h2><p>PLAN: ask several models and compare their answers on the right.<br>"
            "Vote ▲/▼ on replies, then <b>★ Pick</b> to switch to BUILD.</p>"
            "<p>Files the builder changes show up here as diffs. Double-click a file on the left to view it.</p>"
        )
        welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome.setWordWrap(True)
        self.addTab(welcome, "Welcome")

    def _find(self, key: str) -> int:
        for i in range(self.count()):
            if self.widget(i).property("key") == key:
                return i
        return -1

    def open_file(self, path: str) -> None:
        def show(result: dict[str, Any]) -> None:
            key = f"file:{path}"
            i = self._find(key)
            if i < 0:
                view = QTextBrowser()
                view.setProperty("key", key)
                i = self.addTab(view, path.split("/")[-1])
                self.setTabToolTip(i, path)
            self.widget(i).setHtml(code_html(path, result["content"]))
            self.setCurrentIndex(i)

        self.ctl.call(self.ctl.client.file(self.ctl.st.session_id, path), show)

    def show_diff(self, path: str, diff: str, *, call_id: str | None = None, turn_id: str | None = None, model_id: int | None = None) -> DiffView:
        key = f"approval:{call_id}" if call_id else f"diff:{turn_id}:{path}"
        i = self._find(key)
        who = self.ctl.st.name(model_id) if model_id else ""
        title = f"{'Proposed edit' if call_id else 'Changed'}: {path}" + (f"  ({who})" if who else "")
        if i >= 0:
            view = self.widget(i)
            view.set_diff(diff)  # type: ignore[attr-defined]
        else:
            view = DiffView(self.ctl, path, diff, call_id=call_id, turn_id=turn_id, title=title)
            view.setProperty("key", key)
            i = self.addTab(view, ("✎ " if call_id else "Δ ") + path.split("/")[-1])
            self.setTabToolTip(i, title)
        self.setCurrentIndex(i)
        return view  # type: ignore[return-value]

    def approval_resolved(self, call_id: str) -> None:
        for i in range(self.count()):
            w = self.widget(i)
            if isinstance(w, DiffView):
                w.approval_resolved(call_id)
