"""Main window (design C): activity bar · file tree · code/diff editor · chat with model tabs · token status bar."""

from __future__ import annotations

import asyncio
import html
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDockWidget,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QToolBar,
    QWidget,
)

from multichat_desktop import settings
from multichat_desktop.chat import ChatPanel
from multichat_desktop.controller import Controller
from multichat_desktop.dialogs import EndpointsDialog, ModelsDialog, OpenFolderDialog, RunnerDialog
from multichat_desktop.stats import StatsWindow
from multichat_desktop.theme import colors
from multichat_desktop.workspace import EditorTabs, FileTree


def app_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    for i, color in enumerate(("#2563eb", "#d97706", "#059669")):
        p.setBrush(QColor(color))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(4 + i * 16, 18 + (i % 2) * 8, 26, 26)
    p.end()
    return QIcon(pix)


class MainWindow(QMainWindow):
    def __init__(self, ctl: Controller) -> None:
        super().__init__()
        self.ctl = ctl
        self.stats_window: StatsWindow | None = None
        self.quitting = False
        self.setWindowTitle("MultiChat")
        self.setWindowIcon(app_icon())
        self.resize(1500, 920)

        # center: editor tabs
        self.editor = EditorTabs(ctl)
        self.setCentralWidget(self.editor)

        # left: files / sessions
        self.files = FileTree(ctl)
        self.files.open_file.connect(self.editor.open_file)
        self.files.open_folder.connect(self.open_folder)
        self.sessions = QListWidget()
        self.sessions.itemActivated.connect(self._session_chosen)
        self.sessions.itemDoubleClicked.connect(self._session_chosen)
        self.left_stack = QStackedWidget()
        self.left_stack.addWidget(self.files)
        self.left_stack.addWidget(self.sessions)
        self.left = QDockWidget("Files")
        self.left.setWidget(self.left_stack)
        self.left.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.left)

        # right: chat
        self.chat = ChatPanel(ctl)
        self.right = QDockWidget("Chat")
        self.right.setWidget(self.chat)
        self.right.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.right)
        self.resizeDocks([self.left, self.right], [260, 560], Qt.Orientation.Horizontal)

        self._build_activity_bar()
        self._build_toolbar()

        # status bar: per-model tokens + runner
        self.usage_label = QLabel()
        self.usage_label.setTextFormat(Qt.TextFormat.RichText)
        self.runner_label = QLabel("runner off")
        self.statusBar().addWidget(self.usage_label, 1)
        self.statusBar().addPermanentWidget(self.runner_label)

        self._build_tray()
        self.tree_timer = QTimer(self)
        self.tree_timer.setSingleShot(True)
        self.tree_timer.setInterval(400)
        self.tree_timer.timeout.connect(self.files.reload)

        ctl.session_loaded.connect(self.on_session_loaded)
        ctl.event.connect(self.on_event)
        ctl.runner_status.connect(self.on_runner_status)
        ctl.error.connect(lambda msg: self.statusBar().showMessage(f"⚠ {msg}", 10000))

    # ------------------------------------------------------------------ chrome

    def _build_activity_bar(self) -> None:
        bar = QToolBar("Activity")
        bar.setObjectName("activity")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        bar.setIconSize(QSize(1, 1))
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, bar)
        group = QActionGroup(self)
        self.act_files = QAction("📁\nFiles", self, checkable=True, checked=True)
        self.act_sessions = QAction("💬\nSessions", self, checkable=True)
        for a in (self.act_files, self.act_sessions):
            group.addAction(a)
            bar.addAction(a)
        self.act_files.triggered.connect(lambda: self._left_page(0))
        self.act_sessions.triggered.connect(lambda: self._left_page(1))
        bar.addSeparator()
        for text, slot in (("🔌\nEndpoints", self.open_endpoints), ("🖥\nRunner", self.open_runner), ("📊\nStats", self.open_stats)):
            a = QAction(text, self)
            a.triggered.connect(slot)
            bar.addAction(a)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Session")
        tb.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self.title_label = QLabel()
        self.title_label.setStyleSheet("font-weight: bold; padding: 0 8px;")
        tb.addWidget(self.title_label)
        self.folder_btn = QPushButton("No folder · Open…")
        self.folder_btn.clicked.connect(self.open_folder)
        tb.addWidget(self.folder_btn)
        self.models_btn = QPushButton("Models")
        self.models_btn.clicked.connect(self.open_models)
        tb.addWidget(self.models_btn)
        tb.addSeparator()

        self.plan_btn = QPushButton("PLAN")
        self.build_btn = QPushButton("BUILD")
        self.phase_group = QButtonGroup(self)
        for b in (self.plan_btn, self.build_btn):
            b.setCheckable(True)
            b.setObjectName("chip")
            self.phase_group.addButton(b)
            tb.addWidget(b)
        self.plan_btn.clicked.connect(lambda: self.ctl.update_session(phase="plan"))
        self.build_btn.clicked.connect(self._to_build)
        tb.addWidget(QLabel("  Builder:"))
        self.builder_box = QComboBox()
        self.builder_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.builder_box.setMinimumContentsLength(18)
        self.builder_box.activated.connect(self._builder_chosen)
        tb.addWidget(self.builder_box)
        tb.addSeparator()
        self.mode_box = QComboBox()
        self.mode_box.addItem("Isolated — models answer independently", "isolated")
        self.mode_box.addItem("Shared — models see each other", "shared")
        self.mode_box.activated.connect(lambda i: self.ctl.update_session(context_mode=self.mode_box.itemData(i)))
        tb.addWidget(self.mode_box)
        tb.addSeparator()
        tb.addWidget(QLabel("Edits:"))
        self.edits_box = QComboBox()
        self.edits_box.addItems(["auto", "ask"])
        self.edits_box.activated.connect(lambda i: self.ctl.update_session(approve_edits=self.edits_box.itemText(i)))
        tb.addWidget(self.edits_box)
        tb.addWidget(QLabel(" Commands:"))
        self.cmds_box = QComboBox()
        self.cmds_box.addItems(["ask", "auto"])
        self.cmds_box.activated.connect(lambda i: self.ctl.update_session(approve_commands=self.cmds_box.itemText(i)))
        tb.addWidget(self.cmds_box)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.new_btn = QPushButton("＋ New session")
        self.new_btn.clicked.connect(self.new_session)
        tb.addWidget(self.new_btn)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        menu.addAction("Show MultiChat", self.showNormal)
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("MultiChat")
        self.tray.activated.connect(lambda _: self.showNormal())
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _left_page(self, index: int) -> None:
        self.left_stack.setCurrentIndex(index)
        self.left.setWindowTitle(["Files", "Sessions"][index])
        if index == 1:
            self.ctl.call(self.ctl.client.sessions(), self._fill_sessions)

    def _fill_sessions(self, sessions: list[dict]) -> None:
        self.sessions.clear()
        for s in sessions:
            names = ", ".join(p["display_name"] for p in s["participants"])
            cost = s.get("cost") or 0
            spent = "$0" if not cost else (f"${cost:.4f}" if cost < 1 else f"${cost:.2f}")
            item = QListWidgetItem(f"{s['title']}   {spent}\n{s['phase'].upper()} · {s['context_mode']} · {names}")
            item.setToolTip(f"{s.get('tokens', 0)} tokens, {spent} in this session")
            item.setData(Qt.ItemDataRole.UserRole, s["id"])
            self.sessions.addItem(item)

    def _session_chosen(self, item: QListWidgetItem) -> None:
        self.ctl.call(self.ctl.open_session(item.data(Qt.ItemDataRole.UserRole)))

    # ------------------------------------------------------------------ actions

    def open_folder(self) -> None:
        OpenFolderDialog(self.ctl, self).open()

    def new_session(self) -> None:
        if self.ctl.st.is_empty:
            self.statusBar().showMessage("This session is still empty — type a message to start it.", 4000)
            self.chat.composer.setFocus()
            return

        def done(created: bool) -> None:
            if created:
                ModelsDialog(self.ctl, self, title="Models for the new session").open()

        self.ctl.call(self.ctl.new_session(), done)

    def open_models(self) -> None:
        async def go() -> None:
            await self.ctl.refresh_models()
            ModelsDialog(self.ctl, self).open()

        self.ctl.call(go())

    def open_endpoints(self) -> None:
        async def go() -> None:
            await self.ctl.refresh_models()
            EndpointsDialog(self.ctl, self).open()

        self.ctl.call(go())

    def open_runner(self) -> None:
        dlg = RunnerDialog(self)
        dlg.accepted.connect(self.apply_runner_settings)
        dlg.open()

    def open_stats(self) -> None:
        if self.stats_window is None:
            self.stats_window = StatsWindow(self.ctl)
        else:
            self.stats_window.reload()
        self.stats_window.show()
        self.stats_window.raise_()

    def apply_runner_settings(self) -> None:
        if settings.runner_enabled() and settings.runner_roots():
            confirm = self.confirm_command if settings.runner_confirm() else None
            self.ctl.start_runner(settings.runner_roots(), settings.runner_name() or None, confirm)
        else:
            self.ctl.stop_runner()

    async def confirm_command(self, command: str) -> bool:
        box = QMessageBox(QMessageBox.Icon.Question, "Run command on this PC?", f"A model wants to run:\n\n{command}",
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        box.finished.connect(lambda r: future.done() or future.set_result(r == QMessageBox.StandardButton.Yes))
        box.open()
        return await future

    def _to_build(self) -> None:
        if not self.ctl.st.session.get("builder_model_id"):
            self.statusBar().showMessage("Pick a builder first: ★ on a reply, or the Builder box.", 6000)
            self.plan_btn.setChecked(True)
            return
        self.ctl.update_session(phase="build")

    def _builder_chosen(self, index: int) -> None:
        mid = self.builder_box.itemData(index)
        if mid:
            self.ctl.pick_builder(mid)

    # ------------------------------------------------------------------ updates

    def on_session_loaded(self) -> None:
        self.chat.rebuild()
        self.update_toolbar()
        self.update_status()
        self.files.reload()

    def update_toolbar(self) -> None:
        s, st = self.ctl.st.session, self.ctl.st
        self.title_label.setText(s.get("title", ""))
        self.setWindowTitle(f"MultiChat — {s.get('title', '')}")
        label = s.get("workspace_label") or "No folder"
        self.folder_btn.setText(("📂 " + label) if s.get("workspace_kind", "none") != "none" else "No folder · Open…")
        self.models_btn.setText(f"Models ({len(st.participants)})")
        build = s.get("phase") == "build"
        self.build_btn.setChecked(build)
        self.plan_btn.setChecked(not build)
        self.builder_box.blockSignals(True)
        self.builder_box.clear()
        self.builder_box.addItem("— choose —", None)
        for p in st.participants:
            self.builder_box.addItem(p["display_name"], p["model_id"])
        idx = self.builder_box.findData(s.get("builder_model_id"))
        self.builder_box.setCurrentIndex(max(idx, 0))
        self.builder_box.blockSignals(False)
        self.mode_box.setCurrentIndex(max(self.mode_box.findData(s.get("context_mode")), 0))
        self.edits_box.setCurrentText(s.get("approve_edits", "auto"))
        self.cmds_box.setCurrentText(s.get("approve_commands", "ask"))

    def update_status(self) -> None:
        st, c = self.ctl.st, colors()
        parts = []
        for mid in sorted(st.usage, key=lambda k: -st.usage[k].cost):
            u = st.usage[mid]
            approx = "~" if u.estimated else ""
            total = u.input_tokens + u.output_tokens
            tok = f"{total / 1000:.1f}k" if total >= 1000 else str(total)
            parts.append(f'<span style="color:{st.color(mid)}">●</span> {html.escape(st.name(mid))} {approx}{tok} · ${u.cost:.4f}')
        text = "&nbsp;&nbsp;&nbsp;".join(parts) or f'<span style="color:{c["muted"]}">no tokens used in this session yet</span>'
        if parts:
            text += f"&nbsp;&nbsp;&nbsp;<b>Σ ${st.total_cost():.4f}</b>"
        if st.busy:
            text += f'&nbsp;&nbsp;&nbsp;<span style="color:{c["accent"]}">⟳ working…</span>'
        self.usage_label.setText(text)

    def on_runner_status(self, state: str, detail: str) -> None:
        color = {"online": "#22c55e", "connecting": "#eab308", "offline": "#ef4444", "error": "#ef4444"}.get(state, colors()["muted"])
        self.runner_label.setText(f'<span style="color:{color}">●</span> runner {state}')
        self.runner_label.setToolTip(detail)
        if state == "error":
            self.statusBar().showMessage(f"runner: {detail}", 10000)

    def on_event(self, ev: dict[str, Any]) -> None:
        self.chat.on_event(ev)
        etype = ev.get("type")
        if etype == "session":
            self.update_toolbar()
            if ev["session"].get("workspace_kind") != self._last_kind():
                self.files.reload()
        elif etype in ("usage", "turn_start", "turn_end"):
            self.update_status()
        elif etype == "approval_required" and ev.get("kind") == "edit":
            self.editor.show_diff(ev.get("path", ""), ev.get("diff", ""), call_id=ev["call_id"], model_id=ev.get("model_id"))
        elif etype == "approval_resolved":
            self.editor.approval_resolved(ev["call_id"])
        elif etype == "file_changed":
            if not ev.get("reverted"):
                self.editor.show_diff(ev["path"], ev.get("diff", ""), turn_id=ev.get("turn_id"), model_id=ev.get("model_id"))
            self.tree_timer.start()
        elif etype == "reverted":
            self.statusBar().showMessage("Reverted: " + ", ".join(ev.get("restored", [])), 8000)
            self.tree_timer.start()
        elif etype in ("error", "notice"):
            who = self.ctl.st.name(ev.get("model_id")) if ev.get("model_id") else "server"
            self.statusBar().showMessage(f"{who}: {ev.get('message')}", 10000)
        self._kind = self.ctl.st.session.get("workspace_kind")

    def _last_kind(self) -> str | None:
        return getattr(self, "_kind", None)

    # ------------------------------------------------------------------ lifecycle

    def quit(self) -> None:
        self.quitting = True
        self.close()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if not self.quitting and self.ctl.runner and self.tray.isVisible():
            # keep the runner alive in the tray so models can keep working on this PC
            self.hide()
            self.tray.showMessage("MultiChat", "Still running in the tray so the runner stays available.")
            event.ignore()
            return
        event.accept()
