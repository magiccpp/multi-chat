"""Offscreen end-to-end test of the desktop app against a real server process.

Needs a Postgres reachable via MULTICHAT_TEST_DATABASE_URL (default: local multichat_desktop_test).
"""

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import qasync  # noqa: E402
from PySide6.QtCore import QEvent, QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMainWindow, QMenu, QToolButton, QWidget  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
TOKEN = "desktop-test-token"
DB_URL = os.environ.get(
    "MULTICHAT_TEST_DATABASE_URL", "postgresql+asyncpg://multichat:multichat@localhost:5432/multichat_desktop_test"
)
SHOTS = Path(os.environ.get("MULTICHAT_SCREENSHOTS", "/tmp/multichat-screens"))

CONFIG = """
endpoints:
  - name: demo
    kind: mock
    base_url: mock://local?delay=0.01
    models:
      - {name: mock-alpha, display_name: GPT-ish, color: "#10a37f", input_price: 1.25, output_price: 10}
      - {name: mock-beta, display_name: Claude-ish, color: "#d97706", input_price: 4, output_price: 20}
      - {name: mock-gamma, display_name: DeepSeek-ish, color: "#2563eb", input_price: 0.3, output_price: 1.2}
  - name: keyless
    kind: openai_compatible
    base_url: https://api.example.com/v1
    models:
      - {name: some-model, display_name: Needs a key, color: "#6b7280"}
defaults: {approve_edits: ask, approve_commands: auto}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    d = tmp_path_factory.mktemp("srv")
    (d / "config.yaml").write_text(CONFIG)
    env = {
        **os.environ,
        "DATABASE_URL": DB_URL,
        "MULTICHAT_API_TOKEN": TOKEN,
        "CONFIG_FILE": str(d / "config.yaml"),
        "DATA_DIR": str(d / "data"),
    }
    server_dir = REPO / "server"
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"], cwd=server_dir, env=env, check=False, capture_output=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=server_dir, env=env, check=True, capture_output=True)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning", "--timeout-graceful-shutdown", "2"],
        cwd=server_dir,
        env=env,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


class StrayWindows(QObject):
    """Records widgets that show up as their own top-level window although they aren't dialogs, menus or the main
    window: a card made visible before it was put in its list flashes on screen as a separate window."""

    ALLOWED = (QDialog, QMainWindow, QMenu)

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    def eventFilter(self, obj, event):  # noqa: N802 - Qt API
        if (event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow()
                and not isinstance(obj, self.ALLOWED) and type(obj).__name__ not in ("StatsWindow", "QTipLabel")
                and not obj.inherits("QComboBoxPrivateContainer")):
            self.seen.append(type(obj).__name__)
        return False


@pytest.fixture(autouse=True)
def no_stray_windows():
    app = QApplication.instance() or QApplication([])
    watcher = StrayWindows()
    app.installEventFilter(watcher)
    yield
    app.removeEventFilter(watcher)
    assert watcher.seen == [], f"widgets flashed up as separate windows: {watcher.seen}"


def run_async(coro):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def until(cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def test_desktop_flow(server, tmp_path, monkeypatch):
    SHOTS.mkdir(parents=True, exist_ok=True)
    work = tmp_path / "project"
    work.mkdir()
    (work / "auth.py").write_text("def refresh(token):\n    return None\n")
    monkeypatch.setenv("HOME", str(tmp_path))  # isolate QSettings
    from multichat_desktop import settings

    settings.save_runner(True, [str(work)], "desktop-test-pc", False)

    async def scenario():
        from multichat_client.client import MultiChatClient
        from multichat_desktop import theme
        from multichat_desktop.chat import ApprovalCard
        from multichat_desktop.controller import Controller
        from multichat_desktop.main_window import MainWindow
        from multichat_desktop.workspace import DiffView

        theme.apply(QApplication.instance())
        api = MultiChatClient(server, TOKEN)
        models = {m["model_name"]: m["id"] for m in await api.models()}
        s = await api.create_session("Token refresh bug", [models["mock-alpha"], models["mock-beta"], models["mock-gamma"]], "relay")
        ctl = Controller(api)
        win = MainWindow(ctl)
        win.resize(1500, 920)
        win.show()
        await ctl.open_session(s["id"])
        win.apply_runner_settings()
        await until(lambda: ctl.runner_state == "online")

        # Runner dialog: which agent CLIs this PC has; Detect again looks once more
        from multichat_desktop.dialogs import RunnerDialog

        rdlg = RunnerDialog(win, ctl)
        assert "Cursor" in rdlg.clis.text() and rdlg.detect_btn.isEnabled()
        rdlg.clis.setText("stale")
        rdlg.detect_btn.click()
        await until(lambda: "Cursor" in rdlg.clis.text() and "Looking" not in rdlg.clis.text())
        rdlg.reject()
        # a session without a folder uses this PC automatically, so models can inspect it in PLAN
        await until(lambda: ctl.st.session.get("workspace_kind") == "runner")
        assert ctl.st.session["workspace_path"] == str(work.resolve())
        # an explicit "No folder" is kept
        settings.set_no_folder(s["id"], True)
        ctl.set_session(await api.set_workspace(s["id"], "none"))
        assert not await ctl.attach_this_pc() and ctl.st.session["workspace_kind"] == "none"
        settings.set_no_folder(s["id"], False)
        assert await ctl.attach_this_pc()

        # PLAN in shared mode: three models answer, each seeing the previous ones
        win.chat.composer.setPlainText("Users are logged out after refresh. Where is the bug?")
        win.chat._send_clicked()
        await until(lambda: not ctl.st.busy and len([m for m in ctl.st.messages.values() if m.role == "assistant"]) == 3)
        await asyncio.sleep(0.3)
        replies = [m for m in ctl.st.ordered() if m.role == "assistant"]
        assert "I read 2 other answers" in replies[2].content
        # session cost: per model in the cost bar and tabs, per reply on the cards, total in the cost bar
        await until(lambda: all(ctl.st.messages[r.id].usage for r in replies))
        await asyncio.sleep(0.2)
        bar_text = " ".join(w.text() for w in win.chat.cost_bar.findChildren(QLabel) + win.chat.cost_bar.findChildren(QToolButton))
        assert f"Σ ${ctl.st.total_cost():.4f}" in bar_text
        for r in replies:
            assert ctl.st.name(r.model_id) in bar_text
            assert "tok · $" in win.chat.cards[r.id].title.text()
        assert all("$" in win.chat.tabs.tabText(i) for i in range(win.chat.tabs.count()))
        win.grab().save(str(SHOTS / "desktop-plan.png"))
        # the panels can be made narrow: no row of chips forces a minimum width (the divider would barely move)
        assert win.right.minimumSizeHint().width() < 480, win.right.minimumSizeHint().width()
        # Details…: per-model breakdown
        win.chat.show_cost_details()
        await until(lambda: getattr(win.chat, "cost_dialog", None) is not None)
        table = win.chat.cost_dialog.table
        assert table.rowCount() == 4  # three models + total
        names = [table.item(r, 0).text() for r in range(3)]
        assert {n.lstrip("● ") for n in names} == {"GPT-ish", "Claude-ish", "DeepSeek-ish"}
        assert all(table.item(r, 1).text() == "1" for r in range(3))  # one reply each
        assert table.item(3, 8).text() == f"${ctl.st.total_cost():.4f}"
        assert all(table.item(r, 6).text().startswith("$") and table.item(r, 7).text().startswith("$") for r in range(3))
        await asyncio.sleep(0.2)
        win.chat.cost_dialog.grab().save(str(SHOTS / "desktop-cost-details.png"))
        win.chat.cost_dialog.close()

        # vote with the card buttons
        win.chat.cards[replies[1].id].up.click()
        win.chat.cards[replies[0].id].down.click()
        await until(lambda: ctl.st.messages[replies[1].id].vote == 1 and ctl.st.messages[replies[0].id].vote == -1)

        # Copy puts the reply on the clipboard and says so
        card = win.chat.cards[replies[1].id]
        card.copy.click()
        assert QApplication.clipboard().text() == ctl.st.messages[replies[1].id].content
        assert card.copy.text() == "✓ Copied"

        # ★ pick as builder -> BUILD
        win.chat.cards[replies[1].id].builder.click()
        await until(lambda: ctl.st.session.get("phase") == "build")
        assert win.build_btn.isChecked() and win.builder_box.currentData() == replies[1].model_id

        # attach this PC's folder through the runner
        await ctl.client.set_workspace(s["id"], "runner", str(work), runner_id=ctl.runner.runner_id)
        await until(lambda: ctl.st.session.get("workspace_kind") == "runner")
        await until(lambda: win.files.tree.topLevelItemCount() == 1)
        assert "auth.py" in win.files.tree.topLevelItem(0).text(0)

        # BUILD: the edit needs approval -> a diff tab with Accept
        ctl.send('Fix it\n@tool edit_file {"path": "auth.py", "old_string": "return None", "new_string": "return renew(token)"}\n'
                 '@tool run_command {"command": "cat auth.py"}')
        await until(lambda: any(isinstance(win.editor.widget(i), DiffView) and win.editor.widget(i).accept_btn.isVisible()
                                for i in range(win.editor.count())))
        assert any(isinstance(w, ApprovalCard) for w in win.chat.approval_cards.values())
        # the approval is the last item in the chat list and scrolled into view
        approval = next(iter(win.chat.approval_cards.values()))
        assert win.chat.list.indexOf(approval) == win.chat.list.count() - 2  # just above the trailing stretch
        await asyncio.sleep(0.3)
        viewport = win.chat.scroll.viewport()
        top = approval.mapTo(viewport, approval.rect().topLeft()).y()
        assert 0 <= top and top + approval.height() <= viewport.height(), (top, approval.height(), viewport.height())
        # the wait bar says what the turn is waiting for
        await until(lambda: win.chat.wait_bar.isVisible())
        tip = win.chat.wait_bar.rows.itemAt(0).widget().text()
        assert "waiting for your approval to change auth.py" in tip, tip
        await asyncio.sleep(0.2)
        win.grab().save(str(SHOTS / "desktop-approval.png"))
        diff = next(win.editor.widget(i) for i in range(win.editor.count()) if isinstance(win.editor.widget(i), DiffView))
        diff.accept_btn.click()
        await until(lambda: not ctl.st.busy and "renew(token)" in (work / "auth.py").read_text())
        await asyncio.sleep(0.4)
        assert any("renew(token)" in r.content for r in ctl.st.results.values())
        assert "Σ $" in win.usage_label.text()
        win.grab().save(str(SHOTS / "desktop-build.png"))

        # revert from the applied-diff tab
        applied = [win.editor.widget(i) for i in range(win.editor.count())
                   if isinstance(win.editor.widget(i), DiffView) and win.editor.widget(i).revert_btn.isVisible()]
        assert applied
        applied[0].revert_btn.click()
        await until(lambda: "return None" in (work / "auth.py").read_text())

        # statistics window
        win.open_stats()
        await until(lambda: win.stats_window.table.rowCount() == 3)
        await asyncio.sleep(0.3)
        win.stats_window.grab().save(str(SHOTS / "desktop-stats.png"))
        stats = await api.stats()
        row = {r["model_id"]: r for r in stats["models"]}
        assert row[replies[1].model_id]["upvotes"] == 1 and row[replies[1].model_id]["builder_picks"] == 1
        assert row[replies[0].model_id]["downvotes"] == 1

        win.quitting = True
        win.close()
        win.stats_window.close()
        await ctl.shutdown()

    run_async(scenario())


def test_availability_new_session_and_attachments(server, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    notes = tmp_path / "notes.md"
    notes.write_text("# Notes\nThe token expires after 15 minutes.\n")

    async def scenario():
        from PySide6.QtCore import QMimeData, Qt, QUrl
        from PySide6.QtGui import QColor, QImage

        from multichat_client.client import MultiChatClient
        from multichat_desktop import theme
        from multichat_desktop.attachments import AttachmentView
        from multichat_desktop.controller import Controller
        from multichat_desktop.dialogs import EndpointsDialog, ModelsDialog
        from multichat_desktop.main_window import MainWindow

        theme.apply(QApplication.instance())
        api = MultiChatClient(server, TOKEN)
        statuses = {r["name"]: r["status"] for r in await api.check_endpoints()}
        assert statuses == {"demo": "ok", "keyless": "no_key"}
        models = {m["model_name"]: m for m in await api.models()}
        assert not models["some-model"]["available"] and models["mock-alpha"]["available"]

        s = await api.create_session("Attachments", [models["mock-alpha"]["id"]])
        ctl = Controller(api)
        win = MainWindow(ctl)
        win.resize(1300, 860)
        win.show()
        await ctl.open_session(s["id"])

        # effort and context window per model: chosen in the models dialog while the session is still empty
        await api.check_endpoints()
        await ctl.refresh_models()
        dlg = ModelsDialog(ctl, win, title="Models for the new session")
        alpha_item = next(item for _, item in dlg._items() if item.data(0, Qt.ItemDataRole.UserRole) == models["mock-alpha"]["id"])
        ctx_box, eff_box = dlg.tree.itemWidget(alpha_item, 3), dlg.tree.itemWidget(alpha_item, 4)
        # the mock models top out at 128K: nothing to choose
        assert not ctx_box.isEnabled() and [ctx_box.itemText(i) for i in range(ctx_box.count())] == ["Max: 128K (128,000 tokens)"]
        eff_box.setCurrentIndex(eff_box.findData("high"))
        dlg._save()
        # a model with more than 512K offers 256K (the default for a new session) or its maximum
        beta = next(m for m in ctl.models if m["model_name"] == "mock-beta")
        beta["context_length"] = 1_000_000
        big = ModelsDialog(ctl, win, title="Models for the new session")
        beta_item = next(item for _, item in big._items() if item.data(0, Qt.ItemDataRole.UserRole) == beta["id"])
        beta_item.setCheckState(0, Qt.CheckState.Checked)
        box = big.tree.itemWidget(beta_item, 3)
        assert box.isEnabled() and [box.itemText(i) for i in range(box.count())] == ["256K", "Max: 1M (1,000,000 tokens)"]
        assert box.currentData() == 256_000 and big.settings[beta["id"]]["context_window"] == 256_000
        box.setCurrentIndex(1)
        assert big.settings[beta["id"]]["context_window"] == 1_000_000
        big.reject()
        beta["context_length"] = 128_000
        await until(lambda: (ctl.st.participant(models["mock-alpha"]["id"]) or {}).get("effort") == "high")
        assert "Effort: High" in win.chat.tabs.tabToolTip(1)

        # the unavailable model is greyed out and can't be ticked; search filters the list
        dlg = ModelsDialog(ctl, win)
        items = {item.data(0, Qt.ItemDataRole.UserRole): item for _, item in dlg._items()}
        grey = items[models["some-model"]["id"]]
        assert not grey.flags() & Qt.ItemFlag.ItemIsUserCheckable and not grey.flags() & Qt.ItemFlag.ItemIsEnabled
        assert "no API key" in grey.toolTip(0)
        assert items[models["mock-beta"]["id"]].flags() & Qt.ItemFlag.ItemIsUserCheckable
        dlg.filter.setText("beta")
        assert [i.isHidden() for i in (items[models["mock-alpha"]["id"]], items[models["mock-beta"]["id"]])] == [True, False]
        dlg.filter.clear()
        dlg.show()
        await asyncio.sleep(0.2)
        dlg.grab().save(str(SHOTS / "desktop-models.png"))
        dlg.reject()

        eps = EndpointsDialog(ctl, win)
        await until(lambda: eps.list.count() == 2)
        eps.list.setCurrentRow(next(i for i in range(eps.list.count()) if "keyless" in eps.list.item(i).text()))
        assert "No API key" in eps.ep_status.text()
        eps.close()

        # New session on an empty session does nothing
        win.new_session()
        await asyncio.sleep(0.3)
        assert ctl.st.session_id == s["id"]
        assert len([x for x in await api.sessions() if x["title"] == "New session"]) == 0

        # paste an image from the clipboard and drop a document into the composer
        img = QImage(320, 200, QImage.Format.Format_RGB32)
        img.fill(QColor("#2563eb"))
        pasted = QMimeData()
        pasted.setImageData(img)
        assert win.chat.composer.canInsertFromMimeData(pasted)
        win.chat.composer.insertFromMimeData(pasted)
        dropped = QMimeData()
        dropped.setUrls([QUrl.fromLocalFile(str(notes))])
        win.chat.composer.insertFromMimeData(dropped)
        text = QMimeData()
        text.setText("plain text still pastes as text")
        win.chat.composer.insertFromMimeData(text)
        assert [a.filename.split("-")[0] for a in win.chat.pending] == ["pasted", "notes.md"]
        assert win.chat.composer.toPlainText() == "plain text still pastes as text"
        assert win.chat.pending_widget.isVisibleTo(win)
        await asyncio.sleep(0.2)
        win.grab().save(str(SHOTS / "desktop-attach-pending.png"))

        win.chat.composer.setPlainText("What do you see?")
        win.chat._send_clicked()
        assert not win.chat.pending and not win.chat.pending_widget.isVisibleTo(win)
        await until(lambda: not ctl.st.busy and any(m.role == "assistant" and m.status == "complete" for m in ctl.st.messages.values()))
        user = next(m for m in ctl.st.ordered() if m.role == "user")
        assert [a["kind"] for a in user.attachments] == ["image", "document"]
        reply = next(m for m in ctl.st.ordered() if m.role == "assistant")
        assert "(I see 1 image.)" in reply.content and "notes.md" in reply.content
        assert "(effort: high)" in reply.content
        # after the first message the context window is fixed; effort still changes
        dlg = ModelsDialog(ctl, win)
        alpha_item = next(item for _, item in dlg._items() if item.data(0, Qt.ItemDataRole.UserRole) == models["mock-alpha"]["id"])
        assert not dlg.tree.itemWidget(alpha_item, 3).isEnabled() and dlg.tree.itemWidget(alpha_item, 4).isEnabled()
        dlg.reject()
        view = win.chat.cards[user.id].attachments
        assert isinstance(view, AttachmentView)
        await until(lambda: user.attachments[0]["id"] in ctl.attachment_cache)
        await asyncio.sleep(0.3)
        win.grab().save(str(SHOTS / "desktop-attachments.png"))

        # "# ..." goes into memory instead of to the models; the Memory dialog shows it
        await api.set_memory("")
        win.chat.composer.setPlainText("# the prod host is nuc2")
        win.chat._send_clicked()
        await asyncio.sleep(0.5)
        assert (await api.memory())["text"] == "- the prod host is nuc2"
        assert not any("nuc2" in m.content for m in ctl.st.messages.values())  # not sent
        win.open_memory()
        await until(lambda: getattr(win, "memory_dialog", None) is not None)
        assert "nuc2" in win.memory_dialog.text.toPlainText()
        win.memory_dialog.text.setPlainText("")
        win.memory_dialog._save()
        await asyncio.sleep(0.3)
        assert (await api.memory())["text"] == ""

        # Context…: per-model usage, Compact now adds a "context compacted" card
        ctl.send("A follow-up question")
        await until(lambda: not ctl.st.busy and len([m for m in ctl.st.messages.values() if m.role == "assistant"]) == 2)
        win.chat.show_context()
        await until(lambda: getattr(win.chat, "context_dialog", None) is not None)
        table = win.chat.context_dialog.table
        assert table.rowCount() == 1 and table.item(0, 2).text() == "128K" and table.item(0, 4).text() == "0"
        win.chat.context_dialog._compact()
        await until(lambda: any(m.role == "summary" for m in ctl.st.messages.values()))
        await asyncio.sleep(0.3)
        summary = next(m for m in ctl.st.messages.values() if m.role == "summary")
        assert "context compacted" in win.chat.cards[summary.id].title.text()
        win.grab().save(str(SHOTS / "desktop-compacted.png"))

        # the sessions list shows what each session cost
        win._left_page(1)
        await until(lambda: win.sessions.count() > 0)
        row = next(win.sessions.item(i).text() for i in range(win.sessions.count())
                   if win.sessions.item(i).data(Qt.ItemDataRole.UserRole) == s["id"])
        assert f"${ctl.st.total_cost():.4f}" in row
        win._left_page(0)

        # now that the session has messages, New session creates one and asks for its models
        win.new_session()
        await until(lambda: ctl.st.session_id != s["id"])
        await until(lambda: any(isinstance(w, ModelsDialog) and w.isVisible() for w in QApplication.topLevelWidgets()))
        new_dlg = next(w for w in QApplication.topLevelWidgets() if isinstance(w, ModelsDialog) and w.isVisible())
        assert new_dlg.windowTitle() == "Models for the new session"
        assert new_dlg.selected() == [models["mock-alpha"]["id"]]  # carried over (still available)
        new_dlg.reject()
        first_new = ctl.st.session_id
        win.new_session()  # empty again -> ignored
        await asyncio.sleep(0.3)
        assert ctl.st.session_id == first_new

        win.quitting = True
        win.close()
        await ctl.shutdown()

    run_async(scenario())


def test_show_more_lines_link_works():
    """The tool-output label must let the mouse activate its "show N more lines" / "show less" links."""
    from PySide6.QtCore import Qt

    from multichat_client.state import SessionState
    from multichat_desktop.chat import MessageCard

    QApplication.instance() or QApplication([])

    class Ctl:
        st = SessionState()

    Ctl.st.load({"id": 1, "participants": [{"model_id": 2}]}, [
        {"id": 5, "seq": 1, "role": "assistant", "model_id": 2, "content": "ran it",
         "tool_calls": [{"id": "t1", "name": "run_command", "arguments": {"command": "ls"}}]},
        {"id": 6, "seq": 2, "role": "tool", "model_id": 2, "content": "\n".join(f"line {i}" for i in range(20)), "tool_call_id": "t1"},
    ], [], [{"id": 2, "display_name": "A", "color": "#f00"}])
    card = MessageCard(Ctl, 5)
    assert card.tools.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByMouse
    assert "show 14 more lines" in card.tools.text() and "line 19" not in card.tools.text()
    card.tools.linkActivated.emit("more")
    assert "line 19" in card.tools.text() and "show less" in card.tools.text()
    card.tools.linkActivated.emit("less")
    assert "line 19" not in card.tools.text()
