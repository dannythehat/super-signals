"""Authenticated encryption for MetaAPI credentials used by MT5 connections."""

from __future__ import annotations

from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class BrokerCredentialDecryptionError(ValueError):
    """Raised when an encrypted MetaAPI token cannot be opened."""


class MetaApiTokenCipher:
    """Encrypt only the MetaAPI token; Vantage passwords are never stored locally."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        if not keys:
            raise ValueError("At least one broker credential encryption key is required.")
        try:
            self._fernet = MultiFernet([Fernet(key.encode("ascii")) for key in keys])
        except (TypeError, ValueError) as exc:
            raise ValueError("Broker credential encryption key is invalid.") from exc

    def encrypt(self, token: str) -> bytes:
        if not token:
            raise ValueError("MetaAPI token must not be empty.")
        return self._fernet.encrypt(token.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            plaintext = self._fernet.decrypt(ciphertext)
        except InvalidToken as exc:
            raise BrokerCredentialDecryptionError(
                "The encrypted MetaAPI token could not be decrypted."
            ) from exc
        return plaintext.decode("utf-8")

    def rotate(self, ciphertext: bytes) -> bytes:
        try:
            return self._fernet.rotate(ciphertext)
        except InvalidToken as exc:
            raise BrokerCredentialDecryptionError(
                "The encrypted MetaAPI token could not be rotated."
            ) from exc

    @staticmethod
    def fingerprint(token: str) -> str:
        return sha256(token.encode("utf-8")).hexdigest()
