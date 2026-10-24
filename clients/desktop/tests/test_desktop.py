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
from PySide6.QtWidgets import QApplication  # noqa: E402

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
