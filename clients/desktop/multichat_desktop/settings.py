"""Persisted desktop settings: QSettings for preferences, Windows Credential Manager (keyring) for the token."""

from __future__ import annotations

import logging

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
    return str(qsettings().value("runner/enabled", "false")).lower() == "true"


def runner_roots() -> list[str]:
    value = qsettings().value("runner/roots", [])
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in (value or [])]


def runner_name() -> str:
    return str(qsettings().value("runner/name", "") or "")


def runner_confirm() -> bool:
    return str(qsettings().value("runner/confirm", "false")).lower() == "true"


def save_runner(enabled: bool, roots: list[str], name: str, confirm: bool) -> None:
    s = qsettings()
    s.setValue("runner/enabled", "true" if enabled else "false")
    s.setValue("runner/roots", roots)
    s.setValue("runner/name", name)
    s.setValue("runner/confirm", "true" if confirm else "false")
