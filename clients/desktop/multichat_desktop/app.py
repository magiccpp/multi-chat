"""Desktop entry point: connect to the server, then run the Qt window on an asyncio (qasync) loop."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import qasync
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from multichat_client.client import MultiChatClient
from multichat_desktop import settings, theme
from multichat_desktop.asyncutil import set_error_handler
from multichat_desktop.controller import Controller
from multichat_desktop.dialogs import ConnectDialog
from multichat_desktop.main_window import MainWindow


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    app.setApplicationName("MultiChat")
    app.setOrganizationName("MultiChat")
    app.setQuitOnLastWindowClosed(False)  # the tray keeps the runner alive
    theme.apply(app)

    url = os.environ.get("MULTICHAT_URL") or settings.server_url()
    token = os.environ.get("MULTICHAT_TOKEN") or (settings.token(url) if url else "")
    if not url or not token:
        dlg = ConnectDialog(url, token)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return 0
        url, token = dlg.values()

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    ctl = Controller(MultiChatClient(url, token))
    window = MainWindow(ctl)
    set_error_handler(lambda exc: window.statusBar().showMessage(f"⚠ {getattr(exc, 'detail', exc)}", 10000))
    window.show()

    closing = asyncio.Event()
    app.aboutToQuit.connect(closing.set)

    async def run() -> None:
        try:
            await ctl.open_latest_or_new()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(window, "MultiChat", f"Cannot load from {url}:\n{exc}")
        window.apply_runner_settings()
        if not ctl.st.participants:
            window.open_models()
        await closing.wait()
        await ctl.shutdown()

    with loop:
        loop.run_until_complete(run())
    return 0


def smoke_test() -> int:
    """Start the UI without a server (offscreen) to prove the frozen app has all its modules and Qt plugins.
    Writes a log to %TEMP%\\multichat-smoke.log because a windowed exe has no console."""
    import tempfile
    import traceback
    from pathlib import Path

    log = Path(os.environ.get("MULTICHAT_SMOKE_LOG") or Path(tempfile.gettempdir()) / "multichat-smoke.log")
    if not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QColor, QImage

        from multichat_desktop import attachments, stats  # noqa: F401 - imported to prove they load

        app = QApplication(sys.argv)
        theme.apply(app)
        window = MainWindow(Controller(MultiChatClient("http://127.0.0.1:9", "smoke-test")))
        window.show()
        app.processEvents()
        img = QImage(8, 8, QImage.Format.Format_RGB32)
        img.fill(QColor("#2563eb"))
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        assert img.save(buf, "PNG") and attachments.thumbnail(bytes(buf.data()), 4) is not None, "image plugins missing"
        window.quitting = True
        window.close()
        log.write_text("ok\n", encoding="utf-8")
        return 0
    except Exception:  # noqa: BLE001
        log.write_text(traceback.format_exc(), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(main())
