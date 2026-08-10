"""Authenticated encryption for MetaAPI credentials used by MT5 connections."""

from __future__ import annotations

import os
from hashlib import sha256

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_NON_PRODUCTION_ENVIRONMENTS = {"development", "test"}
_DEVELOPMENT_BROKER_KEY = "tuIy4idOd_n3Orml72uzLPUVnKYVQWC3gRlmTBMLdlM="


class BrokerCredentialConfigurationError(RuntimeError):
    """Raised when broker credential encryption is not configured safely."""


class BrokerCredentialDecryptionError(ValueError):
    """Raised when an encrypted MetaAPI token cannot be opened."""


def get_broker_credential_keys() -> tuple[str, ...]:
    """Load rotation-ready broker credential keys without exposing their values."""

    environment = os.getenv("SUPER_SIGNALS_ENV", "development").strip().lower()
    raw_keys = os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
    if raw_keys:
        keys = tuple(key.strip() for key in raw_keys.split(",") if key.strip())
    elif environment in _NON_PRODUCTION_ENVIRONMENTS:
        keys = (_DEVELOPMENT_BROKER_KEY,)
    else:
        raise BrokerCredentialConfigurationError(
            "SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS must be configured."
        )

    if not keys:
        raise BrokerCredentialConfigurationError(
            "SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS must contain at least one key."
        )
    try:
        for key in keys:
            Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise BrokerCredentialConfigurationError(
            "SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS contains an invalid key."
        ) from exc
    if environment not in _NON_PRODUCTION_ENVIRONMENTS and _DEVELOPMENT_BROKER_KEY in keys:
        raise BrokerCredentialConfigurationError(
            "SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS must not use the development key."
        )
    return keys


class MetaApiTokenCipher:
    """Encrypt only the MetaAPI token; Vantage passwords are never stored locally."""

    def __init__(self, keys: tuple[str, ...]) -> None:
        if not keys:
            raise ValueError("At least one broker credential encryption key is required.")
        self._fernet = MultiFernet([Fernet(key.encode("ascii")) for key in keys])

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
