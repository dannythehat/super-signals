"""Environment-backed application configuration."""

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache

from cryptography.fernet import Fernet

_DEVELOPMENT_FINGERPRINT_SECRET = "development-only-change-me"
_DEVELOPMENT_TELEGRAM_SESSION_KEY = "hoTYhKCc3l5glT5kRTBB3xVxOv4anqXrcYrPekzB5dA="
_NON_PRODUCTION_ENVIRONMENTS = {"development", "test"}
_TELEGRAM_API_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    cors_origins: tuple[str, ...]
    database_url: str
    session_cookie_name: str
    session_ttl_seconds: int
    recovery_ttl_seconds: int
    session_cookie_secure: bool
    session_fingerprint_secret: str
    telegram_api_id: int | None
    telegram_api_hash: str | None
    telegram_session_keys: tuple[str, ...]
    telegram_qr_ttl_seconds: int


def _parse_origins(value: str) -> tuple[str, ...]:
    return tuple(origin.strip() for origin in value.split(",") if origin.strip())


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(variable_name: str, default: str) -> int:
    raw_value = os.getenv(variable_name, default)
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{variable_name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{variable_name} must be a positive integer")
    return value


def _required_non_production_value(
    environment: str,
    variable_name: str,
    development_default: str,
) -> str:
    value = os.getenv(variable_name)
    if environment in _NON_PRODUCTION_ENVIRONMENTS:
        return value or development_default
    if not value:
        raise RuntimeError(f"{variable_name} must be configured for {environment}")
    return value


def _optional_non_production_value(
    environment: str,
    variable_name: str,
) -> str | None:
    value = os.getenv(variable_name)
    if value:
        return value.strip()
    if environment in _NON_PRODUCTION_ENVIRONMENTS:
        return None
    raise RuntimeError(f"{variable_name} must be configured for {environment}")


def _normalize_database_url(value: str) -> str:
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


def _fingerprint_secret(environment: str) -> str:
    secret = _required_non_production_value(
        environment,
        "SUPER_SIGNALS_FINGERPRINT_SECRET",
        _DEVELOPMENT_FINGERPRINT_SECRET,
    )
    if environment not in _NON_PRODUCTION_ENVIRONMENTS and (
        secret == _DEVELOPMENT_FINGERPRINT_SECRET or len(secret) < 32
    ):
        raise RuntimeError(
            "SUPER_SIGNALS_FINGERPRINT_SECRET must be a unique value of at least 32 characters"
        )
    return secret


def _telegram_api_credentials(environment: str) -> tuple[int | None, str | None]:
    raw_api_id = _optional_non_production_value(environment, "TELEGRAM_API_ID")
    api_hash = _optional_non_production_value(environment, "TELEGRAM_API_HASH")
    if raw_api_id is None and api_hash is None:
        return None, None
    if raw_api_id is None or api_hash is None:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured together")
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID must be a positive integer") from exc
    if api_id <= 0:
        raise RuntimeError("TELEGRAM_API_ID must be a positive integer")
    if not _TELEGRAM_API_HASH_PATTERN.fullmatch(api_hash):
        raise RuntimeError("TELEGRAM_API_HASH must be a 32-character hexadecimal value")
    return api_id, api_hash.lower()


def _derive_telegram_session_key(secret: str) -> str:
    if len(secret) < 32:
        raise RuntimeError(
            "SUPER_SIGNALS_TELEGRAM_SESSION_SECRET must contain at least 32 characters"
        )
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _telegram_session_keys(environment: str) -> tuple[str, ...]:
    raw_keys = os.getenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS")
    generated_secret = os.getenv("SUPER_SIGNALS_TELEGRAM_SESSION_SECRET")

    if raw_keys and generated_secret:
        raise RuntimeError(
            "Configure only one of SUPER_SIGNALS_TELEGRAM_SESSION_KEYS or "
            "SUPER_SIGNALS_TELEGRAM_SESSION_SECRET"
        )

    if raw_keys:
        keys = tuple(key.strip() for key in raw_keys.split(",") if key.strip())
    elif generated_secret:
        keys = (_derive_telegram_session_key(generated_secret),)
    elif environment in _NON_PRODUCTION_ENVIRONMENTS:
        keys = (_DEVELOPMENT_TELEGRAM_SESSION_KEY,)
    else:
        raise RuntimeError(
            "SUPER_SIGNALS_TELEGRAM_SESSION_KEYS or "
            "SUPER_SIGNALS_TELEGRAM_SESSION_SECRET must be configured"
        )

    if not keys:
        raise RuntimeError(
            "SUPER_SIGNALS_TELEGRAM_SESSION_KEYS must contain at least one Fernet key"
        )
    try:
        for key in keys:
            Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "SUPER_SIGNALS_TELEGRAM_SESSION_KEYS contains an invalid Fernet key"
        ) from exc
    if (
        environment not in _NON_PRODUCTION_ENVIRONMENTS
        and _DEVELOPMENT_TELEGRAM_SESSION_KEY in keys
    ):
        raise RuntimeError("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS must not use the development key")
    return keys


@lru_cache
def get_settings() -> Settings:
    environment = os.getenv("SUPER_SIGNALS_ENV", "development").strip().lower()
    default_secure = environment not in _NON_PRODUCTION_ENVIRONMENTS
    database_url = _normalize_database_url(
        _required_non_production_value(
            environment,
            "DATABASE_URL",
            "postgresql+psycopg://super_signals:super_signals@127.0.0.1:5432/super_signals",
        )
    )
    cors_value = _required_non_production_value(
        environment,
        "SUPER_SIGNALS_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    )
    cors_origins = _parse_origins(cors_value)
    if not cors_origins:
        raise RuntimeError("SUPER_SIGNALS_CORS_ORIGINS must contain at least one origin")
    telegram_api_id, telegram_api_hash = _telegram_api_credentials(environment)

    return Settings(
        environment=environment,
        cors_origins=cors_origins,
        database_url=database_url,
        session_cookie_name=os.getenv(
            "SUPER_SIGNALS_SESSION_COOKIE",
            "super_signals_session",
        ),
        session_ttl_seconds=int(os.getenv("SUPER_SIGNALS_SESSION_TTL_SECONDS", "28800")),
        recovery_ttl_seconds=int(os.getenv("SUPER_SIGNALS_RECOVERY_TTL_SECONDS", "1800")),
        session_cookie_secure=_parse_bool(
            os.getenv("SUPER_SIGNALS_COOKIE_SECURE", str(default_secure))
        ),
        session_fingerprint_secret=_fingerprint_secret(environment),
        telegram_api_id=telegram_api_id,
        telegram_api_hash=telegram_api_hash,
        telegram_session_keys=_telegram_session_keys(environment),
        telegram_qr_ttl_seconds=_positive_int("SUPER_SIGNALS_TELEGRAM_QR_TTL_SECONDS", "120"),
    )
