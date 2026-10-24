"""Persisted desktop settings: QSettings for preferences, Windows Credential Manager (keyring) for the token."""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QSettings

log = logging.getLogger("multichat.desktop")
SERVICE = "MultiChat"


def qsettings() -> QSettings:
    return QSettings("MultiChat", "MultiChat")


def server_url() -> str:
    return str(qsettings().value("server/url", "") or "")


def token(url: str) -> str:
    try:
        import keyring

        value = keyring.get_password(SERVICE, url)
        if value:
            return value
    except Exception:  # noqa: BLE001 - no keyring backend (e.g. headless Linux)
        log.debug("keyring unavailable", exc_info=True)
    return str(qsettings().value("server/token_fallback", "") or "")


def save_server(url: str, tok: str) -> None:
    s = qsettings()
    s.setValue("server/url", url)
    try:
        import keyring

        keyring.set_password(SERVICE, url, tok)
        s.remove("server/token_fallback")
    except Exception:  # noqa: BLE001
        s.setValue("server/token_fallback", tok)


def runner_enabled() -> bool:
    # On by default: models need a machine to inspect (PLAN, read-only) and work on (BUILD, with approvals).
    return str(qsettings().value("runner/enabled", "true")).lower() == "true"


def runner_roots() -> list[str]:
    s = qsettings()
    if not s.contains("runner/roots"):
        return [os.path.expanduser("~")]
    value = s.value("runner/roots", [])
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in (value or [])]


def runner_auto_attach() -> bool:
    """Sessions without a folder use this PC (the first allowed folder) while the runner is online."""
    return str(qsettings().value("runner/auto_attach", "true")).lower() == "true"


def no_folder_sessions() -> set[int]:
    """Sessions where the user explicitly chose "No folder"; auto-attach leaves them alone."""
    value = qsettings().value("runner/no_folder_sessions", [])
    if isinstance(value, str):
        value = [value] if value else []
    return {int(v) for v in value or []}


def set_no_folder(session_id: int, no_folder: bool) -> None:
    ids = no_folder_sessions()
    ids = ids | {session_id} if no_folder else ids - {session_id}
    qsettings().setValue("runner/no_folder_sessions", [str(i) for i in sorted(ids)][-500:])


def runner_name() -> str:
    return str(qsettings().value("runner/name", "") or "")


def runner_confirm() -> bool:
    return str(qsettings().value("runner/confirm", "false")).lower() == "true"


def save_runner(enabled: bool, roots: list[str], name: str, confirm: bool, auto_attach: bool | None = None) -> None:
    s = qsettings()
    if auto_attach is not None:
        s.setValue("runner/auto_attach", "true" if auto_attach else "false")
    s.setValue("runner/enabled", "true" if enabled else "false")
    s.setValue("runner/roots", roots)
    s.setValue("runner/name", name)
    s.setValue("runner/confirm", "true" if confirm else "false")


def save_layout(window) -> None:  # noqa: ANN001 - QMainWindow
    """Window size and panel widths, as the user left them."""
    q = qsettings()
    q.setValue("window/geometry", window.saveGeometry())
    q.setValue("window/state", window.saveState())


def restore_layout(window) -> None:  # noqa: ANN001 - QMainWindow
    q = qsettings()
    geometry, state = q.value("window/geometry"), q.value("window/state")
    if geometry is not None:
        window.restoreGeometry(geometry)
    if state is not None:
        window.restoreState(state)
