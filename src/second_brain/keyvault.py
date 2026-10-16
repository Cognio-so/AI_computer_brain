from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

SALT = b"ai-computer-brain-key-vault"


def derive(master_key: str) -> bytes:
    digest = hashlib.pbkdf2_hmac("sha256", master_key.encode("utf-8"), SALT, 100_000)
    return base64.urlsafe_b64encode(digest)


class KeyVault:
    def __init__(self, master_key: str) -> None:
        self._fernet = Fernet(derive(master_key)) if master_key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def seal(self, plaintext: str) -> str | None:
        if self._fernet is None:
            return None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def open(self, sealed: str | None) -> str | None:
        if not sealed or self._fernet is None:
            return None
        try:
            return self._fernet.decrypt(sealed.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None
