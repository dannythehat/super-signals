"""Password, session-token and privacy-preserving fingerprint helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    if len(password) > 128:
        raise ValueError("Password must not exceed 128 characters.")

    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_BYTES,
        maxmem=128 * 1024 * 1024,
    )
    return (
        f"scrypt$n={_SCRYPT_N}$r={_SCRYPT_R}$p={_SCRYPT_P}${_b64encode(salt)}${_b64encode(derived)}"
    )


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False

    try:
        algorithm, n_part, r_part, p_part, salt_part, hash_part = encoded.split("$")
        if algorithm != "scrypt":
            return False
        n = int(n_part.removeprefix("n="))
        r = int(r_part.removeprefix("r="))
        p = int(p_part.removeprefix("p="))
        salt = _b64decode(salt_part)
        expected = _b64decode(hash_part)
    except (ValueError, TypeError):
        return False

    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=128 * 1024 * 1024,
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def privacy_hash(value: str | None, secret: str) -> str | None:
    if not value:
        return None
    return hmac.new(
        secret.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
