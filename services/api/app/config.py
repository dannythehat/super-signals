"""Environment-backed application configuration."""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    cors_origins: tuple[str, ...]
    database_url: str


def _parse_origins(value: str) -> tuple[str, ...]:
    return tuple(origin.strip() for origin in value.split(",") if origin.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings(
        environment=os.getenv("SUPER_SIGNALS_ENV", "development"),
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
    )
