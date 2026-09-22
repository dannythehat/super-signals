"""Database engine and session helpers."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


@lru_cache
def get_aidy_engine() -> Engine:
    """Dedicated research-only engine that cannot consume the live trading pool.

    AIDY receives one connection maximum, no overflow, and a short pool wait.
    PostgreSQL also enforces server-side statement/lock timeouts so a runaway
    research query dies in the research lane instead of starving live execution.
    """
    url = get_settings().database_url
    kwargs: dict[str, object] = {
        "pool_pre_ping": True,
        "future": True,
    }
    if url.startswith("postgresql"):
        kwargs.update(
            {
                "pool_size": 1,
                "max_overflow": 0,
                "pool_timeout": 1,
                "connect_args": {
                    "options": (
                        "-c statement_timeout=5000 "
                        "-c lock_timeout=1000 "
                        "-c idle_in_transaction_session_timeout=5000"
                    )
                },
            }
        )
    return create_engine(url, **kwargs)


@lru_cache
def get_aidy_session_factory() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_aidy_engine(),
        autoflush=False,
        expire_on_commit=False,
    )


def get_db_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
