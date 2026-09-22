"""Database engine and session helpers."""

from collections.abc import Iterator
from functools import lru_cache
import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    # Production runs several independent background loops. Keep the pool deliberately
    # bounded so a burst of research/reconciliation work cannot swamp the small Render
    # Postgres instance and starve the trading listener. Recycle connections frequently
    # to recover cleanly from Render/Postgres connection resets.
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        pool_recycle=120,
        pool_use_lifo=True,
        pool_size=5,
        max_overflow=3,
        pool_timeout=15,
        future=True,
    )


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@lru_cache
def get_research_engine() -> Engine:
    """Low-priority AIDY/provider-research DB lane.

    Research must never exhaust the live trading pool. It gets one connection, no
    overflow, and a hard PostgreSQL statement timeout so a pathological analytical
    query yields to Telegram -> broker execution instead of monopolising production.
    """
    timeout_ms = _positive_int_env("AIDY_RESEARCH_DB_STATEMENT_TIMEOUT_MS", 5000)
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        pool_recycle=120,
        pool_use_lifo=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
        connect_args={
            "application_name": "super-signals-aidy-research",
            "options": f"-c statement_timeout={timeout_ms} -c lock_timeout=1000",
        },
        future=True,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


@lru_cache
def get_research_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_research_engine(),
        autoflush=False,
        expire_on_commit=False,
    )


def get_db_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
