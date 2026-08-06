"""Environment-backed application configuration."""

import os
from dataclasses import dataclass
from functools import lru_cache


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


@lru_cache
def get_settings() -> Settings:
    environment = os.getenv("SUPER_SIGNALS_ENV", "development")
    default_secure = environment not in {"development", "test"}
    return Settings(
        environment=environment,
        cors_origins=_parse_origins(
            os.getenv(
                "SUPER_SIGNALS_CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            )
        ),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://super_signals:super_signals@127.0.0.1:5432/super_signals",
        ),
        session_cookie_name=os.getenv(
            "SUPER_SIGNALS_SESSION_COOKIE",
            "super_signals_session",
        ),
        session_ttl_seconds=int(os.getenv("SUPER_SIGNALS_SESSION_TTL_SECONDS", "28800")),
        recovery_ttl_seconds=int(os.getenv("SUPER_SIGNALS_RECOVERY_TTL_SECONDS", "1800")),
        session_cookie_secure=_parse_bool(
            os.getenv("SUPER_SIGNALS_COOKIE_SECURE", str(default_secure))
        ),
        session_fingerprint_secret=os.getenv(
            "SUPER_SIGNALS_FINGERPRINT_SECRET",
            "development-only-change-me",
        ),
    )
