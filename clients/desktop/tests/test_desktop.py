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
from PySide6.QtWidgets import QApplication, QLabel, QToolButton  # noqa: E402

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
        s = await api.create_session("Token refresh bug", [models["mock-alpha"], models["mock-beta"], models["mock-gamma"]], "shared")
        ctl = Controller(api)
        win = MainWindow(ctl)
        win.resize(1500, 920)
        win.show()
        await ctl.open_session(s["id"])
        win.apply_runner_settings()
        await until(lambda: ctl.runner_state == "online")

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

        # vote with the card buttons
        win.chat.cards[replies[1].id].up.click()
        win.chat.cards[replies[0].id].down.click()
        await until(lambda: ctl.st.messages[replies[1].id].vote == 1 and ctl.st.messages[replies[0].id].vote == -1)

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
        view = win.chat.cards[user.id].attachments
        assert isinstance(view, AttachmentView)
        await until(lambda: user.attachments[0]["id"] in ctl.attachment_cache)
        await asyncio.sleep(0.3)
        win.grab().save(str(SHOTS / "desktop-attachments.png"))

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
