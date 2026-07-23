"""Key derivation and settings encryption.

One root secret (`PROXSYNC_SECRET_KEY`) produces every other key by HKDF-SHA256 with a
distinct `info` label. Nothing derived is ever persisted, so rotating the root secret
invalidates all sessions and makes stored secrets unreadable — which is the intended
behaviour of a rotation, and is documented as such.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.errors import ConfigurationError

_SALT = b"proxsync/v1"
INFO_JWT = b"jwt-signing"
INFO_SETTINGS = b"settings-encryption"


def derive_key(root_secret: str, info: bytes, length: int = 32) -> bytes:
    if not root_secret:
        raise ConfigurationError("Root secret is not configured")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=_SALT,
        info=info,
    ).derive(root_secret.encode())


def derive_jwt_key(root_secret: str) -> str:
    """Signing key for access tokens, as a URL-safe string."""
    return base64.urlsafe_b64encode(derive_key(root_secret, INFO_JWT)).decode()


class SecretBox:
    """Symmetric encryption for secrets stored in the settings table.

    Values are stored as Fernet tokens (AES-128-CBC + HMAC-SHA256, timestamped). The
    ciphertext is what lands in the database; the plaintext exists only in memory and is
    never returned by the API.
    """

    def __init__(self, root_secret: str) -> None:
        key = base64.urlsafe_b64encode(derive_key(root_secret, INFO_SETTINGS))
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ConfigurationError(
                "A stored secret could not be decrypted. This usually means "
                "PROXSYNC_SECRET_KEY changed; re-enter the affected secrets in Settings."
            ) from exc

    def hint(self, plaintext: str, *, visible: int = 4) -> str:
        """A masked preview safe to show in the UI: ``••••••1234``."""
        if len(plaintext) <= visible:
            return "•" * len(plaintext)
        return "•" * 6 + plaintext[-visible:]
