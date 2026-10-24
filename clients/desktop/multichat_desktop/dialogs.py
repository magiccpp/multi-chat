"""Dialogs: connect, session models, endpoints & models, open folder (this PC / runner / SSH), runner settings."""

from __future__ import annotations

import os
from typing import Any

import httpx
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from multichat_desktop import settings
from multichat_desktop.controller import Controller


class ConnectDialog(QDialog):
    """First run: where is the MultiChat server and what is its token."""

    def __init__(self, url: str = "", token: str = "", error: str = "") -> None:
        super().__init__()
        self.setWindowTitle("Connect to MultiChat server")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        intro = QLabel("Enter the server URL and the API token (MULTICHAT_API_TOKEN in the server's .env).")
        intro.setWordWrap(True)
        form.addRow(intro)
        self.url = QLineEdit(url or "http://localhost:8000")
        self.token = QLineEdit(token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Server URL", self.url)
        form.addRow("API token", self.token)
        self.status = QLabel(error)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #d9534f")
        form.addRow(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _check(self) -> None:
        url, tok = self.url.text().strip().rstrip("/"), self.token.text().strip()
        try:
            r = httpx.get(f"{url}/api/models", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        except httpx.HTTPError as exc:
            self.status.setText(f"Cannot reach {url}: {exc}")
            return
        if r.status_code == 401:
            self.status.setText("The server rejected the token.")
            return
        if r.status_code >= 400:
            self.status.setText(f"Server error {r.status_code}")
            return
        settings.save_server(url, tok)
        self.accept()

    def values(self) -> tuple[str, str]:
        return self.url.text().strip().rstrip("/"), self.token.text().strip()


class ModelsDialog(QDialog):
    """Pick the models (from any endpoint) that join this session; can be changed any time."""

    def __init__(self, ctl: Controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctl = ctl
        self.setWindowTitle("Models in this session")
        self.resize(520, 560)
        layout = QVBoxLayout(self)
        note = QLabel("Tick the models that take part. Agent CLIs (Claude Code, Cursor, Copilot) run where the folder is, "
                      "so they need a folder on this PC (runner) or an SSH host.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Model", "Vendor id", "$ in / out per 1M"])
        layout.addWidget(self.tree, 1)
        current = {p["model_id"] for p in ctl.st.participants}
        groups: dict[str, QTreeWidgetItem] = {}
        for m in ctl.models:
            if not m["enabled"]:
                continue
            group = groups.get(m["endpoint_name"])
            if group is None:
                group = QTreeWidgetItem([m["endpoint_name"] + ("  (agent CLI)" if m["endpoint_kind"] == "agent_cli" else "")])
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                groups[m["endpoint_name"]] = group
                self.tree.addTopLevelItem(group)
            item = QTreeWidgetItem([m["display_name"], m["model_name"], f"{m['input_price']:g} / {m['output_price']:g}"])
            item.setForeground(0, QColor(m["color"]))
            item.setCheckState(0, Qt.CheckState.Checked if m["id"] in current else Qt.CheckState.Unchecked)
            item.setData(0, Qt.ItemDataRole.UserRole, m["id"])
            group.addChild(item)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected(self) -> list[int]:
        ids = []
        for i in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(i)
            for j in range(group.childCount()):
                item = group.child(j)
                if item.checkState(0) == Qt.CheckState.Checked:
                    ids.append(item.data(0, Qt.ItemDataRole.UserRole))
        return ids

    def _save(self) -> None:
        keep = [p["model_id"] for p in self.ctl.st.participants]
        chosen = self.selected()
        self.ctl.set_models([m for m in keep if m in chosen] + [m for m in chosen if m not in keep])
        self.accept()


class EndpointsDialog(QDialog):
    """Endpoints (vendor URL + key) and their models. Rows from config.yaml on the server are read-only."""

    COLS = ["Display name", "Vendor id", "Color", "$ in /1M", "$ out /1M", "Enabled"]

    def __init__(self, ctl: Controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctl = ctl
        self.endpoints: list[dict] = []
        self.presets: list[dict] = []
        self.setWindowTitle("Endpoints and models")
        self.resize(980, 600)
        root = QHBoxLayout(self)
        split = QSplitter()
        root.addWidget(split)

        left = QWidget()
        ll = QVBoxLayout(left)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._select)
        ll.addWidget(self.list, 1)
        self.preset = QComboBox()
        add = QPushButton("Add endpoint")
        add.clicked.connect(self._add_endpoint)
        ll.addWidget(QLabel("New endpoint from preset:"))
        ll.addWidget(self.preset)
        ll.addWidget(add)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        self.readonly_note = QLabel("Managed by config.yaml on the server — edit it there and reload.")
        self.readonly_note.setObjectName("muted")
        rl.addWidget(self.readonly_note)
        form = QFormLayout()
        self.name = QLineEdit()
        self.kind = QLabel()
        self.base_url = QLineEdit()
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("unchanged")
        form.addRow("Name", self.name)
        form.addRow("Kind", self.kind)
        form.addRow("Base URL", self.base_url)
        form.addRow("API key", self.api_key)
        rl.addLayout(form)
        row = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.test_btn = QPushButton("Test")
        self.delete_btn = QPushButton("Delete")
        self.save_btn.clicked.connect(self._save_endpoint)
        self.test_btn.clicked.connect(self._test)
        self.delete_btn.clicked.connect(self._delete)
        for b in (self.save_btn, self.test_btn, self.delete_btn):
            row.addWidget(b)
        row.addStretch(1)
        self.status = QLabel()
        row.addWidget(self.status)
        rl.addLayout(row)

        rl.addWidget(QLabel("Models"))
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemChanged.connect(self._model_edited)
        rl.addWidget(self.table, 1)
        mrow = QHBoxLayout()
        self.new_model = QLineEdit()
        self.new_model.setPlaceholderText("vendor model id, e.g. gpt-5 or openai/gpt-4.1")
        add_model = QPushButton("Add model")
        add_model.clicked.connect(self._add_model)
        import_btn = QPushButton("Import from vendor…")
        import_btn.clicked.connect(self._import)
        mrow.addWidget(self.new_model, 1)
        mrow.addWidget(add_model)
        mrow.addWidget(import_btn)
        rl.addLayout(mrow)
        split.addWidget(right)
        split.setSizes([260, 720])
        self._loading = False
        self.reload()

    def reload(self, select: int | None = None) -> None:
        async def load() -> tuple[list, list]:
            return await self.ctl.client.endpoints(), await self.ctl.client.presets()

        def done(result: tuple[list, list]) -> None:
            self.endpoints, self.presets = result
            self.preset.clear()
            for p in self.presets:
                self.preset.addItem(p["name"], p)
            self.list.clear()
            for e in self.endpoints:
                item = QListWidgetItem(e["name"] + ("" if e["enabled"] else " (disabled)"))
                if e["source"] == "config":
                    item.setToolTip("from config.yaml")
                self.list.addItem(item)
            if self.endpoints:
                index = next((i for i, e in enumerate(self.endpoints) if e["id"] == select), 0)
                self.list.setCurrentRow(index)

        self.ctl.call(load(), done)

    def current(self) -> dict | None:
        row = self.list.currentRow()
        return self.endpoints[row] if 0 <= row < len(self.endpoints) else None

    def _select(self, row: int) -> None:
        e = self.current()
        if e is None:
            return
        editable = e["source"] != "config"
        self.readonly_note.setVisible(not editable)
        self.name.setText(e["name"])
        self.kind.setText(e["kind"] + (f" · {e['driver']}" if e.get("driver") else ""))
        self.base_url.setText(e.get("base_url") or "")
        self.api_key.clear()
        self.api_key.setPlaceholderText(e.get("api_key_masked") or "no key")
        for w in (self.name, self.base_url, self.api_key, self.save_btn, self.delete_btn):
            w.setEnabled(editable)
        self.status.clear()
        self._fill_models(e)

    def _fill_models(self, e: dict) -> None:
        self._loading = True
        models = [m for m in self.ctl.models if m["endpoint_id"] == e["id"]]
        self.table.setRowCount(len(models))
        for r, m in enumerate(models):
            values = [m["display_name"], m["model_name"], m["color"], f"{m['input_price']:g}", f"{m['output_price']:g}", "yes" if m["enabled"] else "no"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, m["id"])
                if m["source"] == "config" or col == 1:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 2:
                    item.setForeground(QColor(m["color"]))
                self.table.setItem(r, col, item)
        self.table.resizeColumnsToContents()
        self._loading = False

    def _model_edited(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        mid = item.data(Qt.ItemDataRole.UserRole)
        col, text = item.column(), item.text().strip()
        field = {0: "display_name", 2: "color", 3: "input_price", 4: "output_price", 5: "enabled"}.get(col)
        if field is None:
            return
        value: Any = text
        if field in ("input_price", "output_price"):
            try:
                value = float(text or 0)
            except ValueError:
                self.status.setText("prices must be numbers")
                return
        elif field == "enabled":
            value = text.lower() in ("yes", "true", "1", "y")
        self.ctl.call(self.ctl.client.update_model(mid, **{field: value}), lambda _: self.ctl.call(self.ctl.refresh_models()))

    def _add_endpoint(self) -> None:
        p = self.preset.currentData()
        if not p:
            return
        name = p["id"]
        taken = {e["name"] for e in self.endpoints}
        n = 2
        while name in taken:
            name, n = f"{p['id']}-{n}", n + 1
        body = {"name": name, "kind": p["kind"], "base_url": p.get("base_url"), "driver": p.get("driver")}
        self.ctl.call(self.ctl.client.create_endpoint(**body), lambda e: self.reload(e["id"]))

    def _save_endpoint(self) -> None:
        e = self.current()
        if e is None:
            return
        body: dict[str, Any] = {"name": self.name.text().strip(), "base_url": self.base_url.text().strip()}
        if self.api_key.text():
            body["api_key"] = self.api_key.text()
        self.ctl.call(self.ctl.client.update_endpoint(e["id"], **body), lambda r: (self.status.setText("saved"), self.reload(r["id"])))

    def _test(self) -> None:
        e = self.current()
        if e:
            self.status.setText("testing…")
            self.ctl.call(self.ctl.client.test_endpoint(e["id"]), lambda r: self.status.setText(("✓ " if r["ok"] else "✗ ") + r["detail"][:120]))

    def _delete(self) -> None:
        e = self.current()
        if e and QMessageBox.question(self, "Delete endpoint", f"Delete {e['name']} and its models?") == QMessageBox.StandardButton.Yes:
            self.ctl.call(self.ctl.client.delete_endpoint(e["id"]), lambda _: (self.ctl.call(self.ctl.refresh_models()), self.reload()))

    def _add_model(self) -> None:
        e, mid = self.current(), self.new_model.text().strip()
        if e and mid:
            self.new_model.clear()
            self.ctl.call(self.ctl.client.create_model(endpoint_id=e["id"], model_name=mid), lambda _: self._after_models_change(e))

    def _after_models_change(self, e: dict) -> None:
        async def go() -> None:
            await self.ctl.refresh_models()
            self._fill_models(e)

        self.ctl.call(go())

    def _import(self) -> None:
        e = self.current()
        if e is None:
            return

        def chooser(available: list[dict]) -> None:
            dlg = QDialog(self)
            dlg.setWindowTitle(f"Import models from {e['name']}")
            dlg.resize(460, 520)
            lay = QVBoxLayout(dlg)
            filt = QLineEdit()
            filt.setPlaceholderText("filter")
            lst = QListWidget()
            for m in available:
                price = f"   ${m['input_price']:.2f}/${m['output_price']:.2f}" if "input_price" in m else ""
                item = QListWidgetItem(m["id"] + price)
                item.setData(Qt.ItemDataRole.UserRole, m["id"])
                item.setCheckState(Qt.CheckState.Unchecked)
                lst.addItem(item)
            filt.textChanged.connect(lambda t: [lst.item(i).setHidden(t.lower() not in lst.item(i).text().lower()) for i in range(lst.count())])
            lay.addWidget(filt)
            lay.addWidget(lst, 1)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            lay.addWidget(bb)
            bb.rejected.connect(dlg.reject)

            def ok() -> None:
                ids = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count()) if lst.item(i).checkState() == Qt.CheckState.Checked]
                dlg.accept()
                if ids:
                    self.ctl.call(self.ctl.client.import_models(e["id"], ids), lambda _: self._after_models_change(e))

            bb.accepted.connect(ok)
            dlg.open()

        self.status.setText("loading vendor model list…")
        self.ctl.call(self.ctl.client.available_models(e["id"]), lambda a: (self.status.clear(), chooser(a)))


class OpenFolderDialog(QDialog):
    """Choose where the session's folder lives: none, this PC (runner), another runner, or an SSH host."""

    def __init__(self, ctl: Controller, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctl = ctl
        self.setWindowTitle("Open folder")
        self.resize(640, 520)
        self.runners: list[dict] = []
        self.ssh: list[dict] = []
        self.browse_path = ""
        layout = QVBoxLayout(self)
        self.group = QButtonGroup(self)
        self.opt_none = QRadioButton("No folder (chat only; builders answer with code)")
        self.opt_pc = QRadioButton("This PC")
        self.opt_runner = QRadioButton("Another runner")
        self.opt_ssh = QRadioButton("SSH host")
        for i, b in enumerate((self.opt_none, self.opt_pc, self.opt_runner, self.opt_ssh)):
            self.group.addButton(b, i)
            layout.addWidget(b)
        self.group.idToggled.connect(self._mode)

        # this PC
        self.pc_box = QWidget()
        pcl = QHBoxLayout(self.pc_box)
        self.pc_path = QLineEdit()
        pick = QPushButton("Browse…")
        pick.clicked.connect(self._pick_local)
        pcl.addWidget(self.pc_path, 1)
        pcl.addWidget(pick)
        layout.addWidget(self.pc_box)

        # other runner / ssh share a browser
        self.remote_box = QWidget()
        rl = QVBoxLayout(self.remote_box)
        top = QHBoxLayout()
        self.target = QComboBox()
        self.target.currentIndexChanged.connect(lambda _: self._browse(""))
        self.add_ssh_btn = QPushButton("Add SSH connection…")
        self.add_ssh_btn.clicked.connect(self._add_ssh)
        top.addWidget(self.target, 1)
        top.addWidget(self.add_ssh_btn)
        rl.addLayout(top)
        self.where = QLabel()
        self.where.setObjectName("muted")
        rl.addWidget(self.where)
        self.dirs = QListWidget()
        self.dirs.itemDoubleClicked.connect(self._enter)
        rl.addWidget(self.dirs, 1)
        self.remote_path = QLineEdit()
        rl.addWidget(self.remote_path)
        layout.addWidget(self.remote_box, 1)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        kind = ctl.st.session.get("workspace_kind", "none")
        mine = ctl.runner.runner_id if ctl.runner else None
        if kind == "ssh":
            self.opt_ssh.setChecked(True)
        elif kind == "runner" and ctl.st.session.get("runner_id") not in (None, mine):
            self.opt_runner.setChecked(True)
        elif kind == "runner":
            self.opt_pc.setChecked(True)
            self.pc_path.setText(ctl.st.session.get("workspace_path") or "")
        else:
            self.opt_none.setChecked(True)
        self._load_targets()

    def _load_targets(self) -> None:
        async def load() -> tuple[list, list]:
            return await self.ctl.client.runners(), await self.ctl.client.ssh_connections()

        def done(result: tuple[list, list]) -> None:
            self.runners, self.ssh = result
            self._mode(self.group.checkedId(), True)

        self.ctl.call(load(), done)

    def _mode(self, button_id: int, checked: bool) -> None:
        if not checked:
            return
        self.pc_box.setVisible(button_id == 1)
        self.remote_box.setVisible(button_id in (2, 3))
        self.add_ssh_btn.setVisible(button_id == 3)
        self.target.blockSignals(True)
        self.target.clear()
        if button_id == 2:
            for r in self.runners:
                self.target.addItem(f"{r['name']}  ({r['os']}, {'online' if r['online'] else 'offline'})", r["id"])
        elif button_id == 3:
            for c in self.ssh:
                fp = c.get("host_key_fingerprint") or "host key not trusted yet"
                self.target.addItem(f"{c['name']}  {c['username']}@{c['host']}:{c['port']}  ·  {fp}", c["id"])
        self.target.blockSignals(False)
        if button_id == 1 and not self.ctl.runner:
            self.status.setText("The runner on this PC is off. Turn it on in Runner settings so models can work here.")
        else:
            self.status.clear()
        if button_id in (2, 3) and self.target.count():
            self._browse("")

    def _pick_local(self) -> None:
        start = self.pc_path.text() or (settings.runner_roots() or [os.path.expanduser("~")])[0]
        path = QFileDialog.getExistingDirectory(self, "Choose folder", start)
        if path:
            self.pc_path.setText(path)

    def _browse(self, path: str) -> None:
        target = self.target.currentData()
        if target is None:
            return
        if self.group.checkedId() == 2:
            coro = self.ctl.client.browse_runner(target, path)
        else:
            conn = next(c for c in self.ssh if c["id"] == target)
            if not conn.get("host_key_fingerprint"):
                self._trust(conn)
                return
            coro = self.ctl.client.browse_ssh(target, path)

        def done(result: dict) -> None:
            self.browse_path = result["path"]
            self.where.setText(result["path"] or "allowed folders")
            self.remote_path.setText(result["path"])
            self.dirs.clear()
            if result["path"]:
                self.dirs.addItem("..")
            for e in result["entries"]:
                self.dirs.addItem(e["name"])

        self.ctl.call(coro, done)

    def _enter(self, item: QListWidgetItem) -> None:
        name = item.text()
        base = self.browse_path
        if name == "..":
            sep = "\\" if "\\" in base else "/"
            parent = base.rstrip(sep).rsplit(sep, 1)[0] or sep
            self._browse(parent if parent != base else "")
        elif not base or os.path.isabs(name) or (len(name) > 1 and name[1] == ":"):
            self._browse(name)
        else:
            sep = "\\" if "\\" in base else "/"
            self._browse(base.rstrip(sep) + sep + name)

    def _trust(self, conn: dict) -> None:
        def scanned(result: dict) -> None:
            answer = QMessageBox.question(
                self,
                "Trust this host?",
                f"{conn['host']}:{conn['port']} presents the host key\n\n{result['fingerprint']}\n\n"
                "Compare it with the server's key (ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub). Trust it?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.ctl.call(
                    self.ctl.client.trust_host_key(conn["id"], result["fingerprint"]),
                    lambda c: (conn.update(c), self._mode(3, True)),
                )

        self.ctl.call(self.ctl.client.scan_host_key(conn["id"]), scanned)

    def _add_ssh(self) -> None:
        dlg = SshDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.ctl.call(self.ctl.client.create_ssh(**dlg.values()), lambda c: (self.ssh.append(c), self._mode(3, True)))

    def _apply(self) -> None:
        sid = self.ctl.st.session_id
        mode = self.group.checkedId()
        if mode == 0:
            coro = self.ctl.client.set_workspace(sid, "none")
        elif mode == 1:
            if not self.ctl.runner or not self.ctl.runner.runner_id:
                self.status.setText("Turn on the runner for this PC first (Runner settings).")
                return
            path = self.pc_path.text().strip()
            roots = [os.path.normcase(os.path.abspath(r)) for r in settings.runner_roots()]
            if not any(os.path.normcase(os.path.abspath(path)).startswith(r) for r in roots):
                self.status.setText("That folder is outside the folders the runner may use. Add it in Runner settings.")
                return
            coro = self.ctl.client.set_workspace(sid, "runner", path, runner_id=self.ctl.runner.runner_id)
        else:
            target = self.target.currentData()
            path = self.remote_path.text().strip()
            if target is None or not path:
                self.status.setText("Choose a target and a folder.")
                return
            if mode == 2:
                coro = self.ctl.client.set_workspace(sid, "runner", path, runner_id=target)
            else:
                coro = self.ctl.client.set_workspace(sid, "ssh", path, ssh_connection_id=target)
        self.ctl.call(coro, lambda s: (self.ctl.set_session(s), self.accept()))


class SshDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add SSH connection")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.host = QLineEdit()
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.user = QLineEdit()
        self.auth = QComboBox()
        self.auth.addItems(["key", "password", "agent"])
        self.key = QPlainTextEdit()
        self.key.setPlaceholderText("-----BEGIN OPENSSH PRIVATE KEY-----  (paste, or load a file)")
        load = QPushButton("Load key file…")
        load.clicked.connect(self._load)
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.shell = QComboBox()
        self.shell.addItems(["bash", "powershell"])
        for label, w in (("Name", self.name), ("Host", self.host), ("Port", self.port), ("User", self.user), ("Auth", self.auth),
                         ("Private key", self.key), ("", load), ("Key passphrase", self.passphrase), ("Password", self.password),
                         ("Remote shell", self.shell)):
            form.addRow(label, w)
        note = QLabel("Secrets are stored encrypted on the server.")
        note.setObjectName("muted")
        form.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Private key", os.path.expanduser("~/.ssh"))
        if path:
            with open(path, encoding="utf-8") as f:
                self.key.setPlainText(f.read())

    def values(self) -> dict[str, Any]:
        return {
            "name": self.name.text().strip() or self.host.text().strip(),
            "host": self.host.text().strip(),
            "port": self.port.value(),
            "username": self.user.text().strip(),
            "auth": self.auth.currentText(),
            "private_key": self.key.toPlainText().strip() or None,
            "passphrase": self.passphrase.text() or None,
            "password": self.password.text() or None,
            "shell": self.shell.currentText(),
        }


class RunnerDialog(QDialog):
    """Let MultiChat run tasks on this PC: which folders, and whether to confirm each command locally."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Runner on this PC")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox("Let MultiChat run tasks on this PC (edit files, run commands, install software)")
        self.enabled.setChecked(settings.runner_enabled())
        layout.addWidget(self.enabled)
        note = QLabel("Commands run with your normal user rights in PowerShell. Installs that need administrator "
                      "rights will fail rather than ask for elevation.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        layout.addWidget(QLabel("Folders the models may use:"))
        self.roots = QListWidget()
        self.roots.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for r in settings.runner_roots():
            self.roots.addItem(r)
        layout.addWidget(self.roots, 1)
        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        rem = QPushButton("Remove")
        add.clicked.connect(self._add)
        rem.clicked.connect(lambda: self.roots.takeItem(self.roots.currentRow()))
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        layout.addLayout(row)
        form = QFormLayout()
        self.name = QLineEdit(settings.runner_name())
        self.name.setPlaceholderText(os.environ.get("COMPUTERNAME") or "this computer's name")
        form.addRow("Runner name", self.name)
        layout.addLayout(form)
        self.confirm = QCheckBox("Also ask me on this PC before every command, even when the session auto-approves")
        self.confirm.setChecked(settings.runner_confirm())
        layout.addWidget(self.confirm)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

    def _add(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Allow folder", os.path.expanduser("~"))
        if path:
            self.roots.addItem(path)

    def _save(self) -> None:
        roots = [self.roots.item(i).text() for i in range(self.roots.count())]
        if self.enabled.isChecked() and not roots:
            QMessageBox.warning(self, "Runner", "Add at least one folder.")
            return
        settings.save_runner(self.enabled.isChecked(), roots, self.name.text().strip(), self.confirm.isChecked())
        self.accept()
