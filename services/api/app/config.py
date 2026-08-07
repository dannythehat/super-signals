"""Environment-backed application configuration."""

import os
from dataclasses import dataclass
from functools import lru_cache

_DEVELOPMENT_FINGERPRINT_SECRET = "development-only-change-me"
_NON_PRODUCTION_ENVIRONMENTS = {"development", "test"}


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


def _parse_origins(value: str) -> tuple[str, ...]:
    return tuple(origin.strip() for origin in value.split(",") if origin.strip())


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


@lru_cache
def get_settings() -> Settings:
    environment = os.getenv("SUPER_SIGNALS_ENV", "development").strip().lower()
    default_secure = environment not in _NON_PRODUCTION_ENVIRONMENTS
    database_url = _required_non_production_value(
        environment,
        "DATABASE_URL",
        "postgresql+psycopg://super_signals:super_signals@127.0.0.1:5432/super_signals",
    )
    cors_value = _required_non_production_value(
        environment,
        "SUPER_SIGNALS_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173",
    )
    cors_origins = _parse_origins(cors_value)
    if not cors_origins:
        raise RuntimeError("SUPER_SIGNALS_CORS_ORIGINS must contain at least one origin")

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
    )
