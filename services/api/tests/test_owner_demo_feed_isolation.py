"""Owner demo poll must use a bounded lane independent of canonical trading."""
from __future__ import annotations

from types import SimpleNamespace

from app import db
from app.routes import local_bridge


def test_owner_feed_pool_is_bounded_and_dedicated(monkeypatch):
    monkeypatch.setattr(
        db, "get_settings",
        lambda: SimpleNamespace(database_url="postgresql+psycopg://test:test@localhost/test"),
    )
    db.get_owner_feed_engine.cache_clear()
    db.get_owner_feed_session_factory.cache_clear()
    try:
        engine = db.get_owner_feed_engine()
        assert engine.pool.size() == 1
        assert engine.pool._max_overflow == 1
        assert engine.pool._timeout == 2
        assert db.get_owner_feed_session_factory().kw["bind"] is engine
        assert local_bridge.get_owner_feed_session_factory is db.get_owner_feed_session_factory
    finally:
        db.get_owner_feed_session_factory.cache_clear()
        db.get_owner_feed_engine.cache_clear()
        engine.dispose()
