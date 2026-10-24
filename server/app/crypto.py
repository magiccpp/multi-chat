"""Encryption at rest for API keys and SSH secrets."""

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
    return Fernet(key.encode())


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
