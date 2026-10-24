import pytest
from cryptography.fernet import Fernet

from app.crypto import fernet_key


def test_accepts_real_fernet_keys_unchanged():
    key = Fernet.generate_key().decode()
    assert fernet_key(key) == key.encode()


@pytest.mark.parametrize("secret", ["a" * 64, "8f1c0e6d2b7a4f3e9d1c5b8a7e6f4d3c2b1a0f9e8d7c6b5a4f3e2d1c0b9a8f7e", "correct horse battery staple"])
def test_derives_a_key_from_other_secrets(secret):
    f = Fernet(fernet_key(secret))
    assert f.decrypt(f.encrypt(b"x")) == b"x"
    assert fernet_key(secret) == fernet_key(secret)  # stable across restarts


def test_rejects_short_secrets():
    with pytest.raises(RuntimeError, match="too short"):
        fernet_key("short")
