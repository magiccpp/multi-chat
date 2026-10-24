"""Encryption at rest for API keys and SSH secrets."""

import base64
import binascii
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    settings = get_settings()
    key = settings.multichat_secret_key.strip()
    if not key:
        path = settings.data_dir / "secret.key"
        if path.exists():
            key = path.read_text().strip()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            path.write_text(key)
            path.chmod(0o600)
    return Fernet(fernet_key(key))


def fernet_key(secret: str) -> bytes:
    """Use a real Fernet key as-is; derive one from any other secret (e.g. `openssl rand -hex 32`)."""
    try:
        if len(base64.urlsafe_b64decode(secret.encode())) == 32:
            return secret.encode()
    except (binascii.Error, ValueError):
        pass
    if len(secret) < 16:
        raise RuntimeError("MULTICHAT_SECRET_KEY is too short; use at least 16 random characters (or leave it empty)")
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def encrypt(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("stored secret cannot be decrypted; was MULTICHAT_SECRET_KEY changed?") from exc


def mask(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "…" + value[-2:]
    return f"{value[:3]}…{value[-4:]}"
