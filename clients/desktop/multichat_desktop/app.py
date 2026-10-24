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


if __name__ == "__main__":
    sys.exit(main())
