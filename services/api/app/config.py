"""Environment-backed application configuration."""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    cors_origins: tuple[str, ...]


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
    )
