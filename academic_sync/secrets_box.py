"""Encrypt the credentials the hosted app keeps in its database.

Locally a token lives in ``token.json`` with mode 0600. Hosted, it lives in a
database row, where anyone who can read a backup or a dashboard could read it.
So it is encrypted with a key that exists only in the deployment's environment
(``TOKEN_ENCRYPTION_KEY``) and never next to the data.

Fails **closed**: a wrong key, a tampered value or a missing key raises
``SecretsError`` rather than returning something plausible. Fernet is
authenticated encryption, so a modified ciphertext is detected, not decrypted
into garbage.
"""

from __future__ import annotations

import os
from typing import Optional


class SecretsError(RuntimeError):
    """The secret could not be encrypted or decrypted. Message says what to fix."""


class SecretsBox:
    def __init__(self, key: Optional[str] = None) -> None:
        key = key if key is not None else os.environ.get("TOKEN_ENCRYPTION_KEY")
        if not key:
            raise SecretsError(
                "TOKEN_ENCRYPTION_KEY is not set. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())\""
            )
        from cryptography.fernet import Fernet

        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise SecretsError(
                "TOKEN_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)"
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise SecretsError(
                "a stored secret could not be decrypted — wrong TOKEN_ENCRYPTION_KEY, "
                "or the value was modified. Reconnect the account to store a fresh one."
            ) from None
