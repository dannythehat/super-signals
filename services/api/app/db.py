"""Database engine and session helpers."""

from collections.abc import Iterator
from functools import lru_cache

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


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
