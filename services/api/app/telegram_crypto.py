"""Authenticated encryption for portable Telegram sessions."""

from __future__ import annotations

from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SessionDecryptionError(ValueError):
    """Raised when an encrypted Telegram session cannot be opened."""


class TelegramSessionCipher:
    """Encrypt Telethon StringSession values with rotation-ready Fernet keys."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        if not keys:
            raise ValueError("At least one Telegram session encryption key is required.")
        self._fernet = MultiFernet([Fernet(key.encode("ascii")) for key in keys])

    def encrypt(self, session_string: str) -> bytes:
        if not session_string:
            raise ValueError("Telegram session must not be empty.")
        return self._fernet.encrypt(session_string.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise SessionDecryptionError(
                "The encrypted Telegram session could not be decrypted."
            ) from exc
        return plaintext.decode("utf-8")

    def rotate(self, ciphertext: bytes) -> bytes:
        try:
            return self._fernet.rotate(ciphertext)
        except InvalidToken as exc:
            raise SessionDecryptionError(
                "The encrypted Telegram session could not be rotated."
            ) from exc

    @staticmethod
    def fingerprint(session_string: str) -> str:
        return sha256(session_string.encode("utf-8")).hexdigest()
