from types import SimpleNamespace

from app import db


def test_research_engine_stays_single_lane_but_waits_for_it(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.delenv("AIDY_RESEARCH_DB_STATEMENT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("AIDY_RESEARCH_DB_POOL_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(
        db,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql+psycopg://example.invalid/db"),
    )
    monkeypatch.setattr(db, "create_engine", fake_create_engine)
    db.get_research_engine.cache_clear()
    try:
        db.get_research_engine()
    finally:
        db.get_research_engine.cache_clear()

    kwargs = captured["kwargs"]
    assert kwargs["pool_size"] == 1
    assert kwargs["max_overflow"] == 0
    assert kwargs["pool_timeout"] == 30
    options = kwargs["connect_args"]["options"]
    assert "statement_timeout=15000" in options
    assert "lock_timeout=1000" in options
    assert "idle_in_transaction_session_timeout=15000" in options
