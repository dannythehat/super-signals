from datetime import UTC, datetime, timedelta
from app.routes.server_monitor import _account_snapshot

NOW = datetime(2026, 11, 6, 4, tzinfo=UTC)


def test_unavailable_positions_are_not_zero():
    r = _account_snapshot("demo", {}, {}, NOW)
    assert r["positions"] is None
    assert r["broker_state"] == "unavailable"
    assert r["components"]["read_positions"]["status"] == "unavailable"


def test_worker_heartbeat_does_not_make_old_positions_current():
    worker = {"demo": {"worker_id": "fixture", "last_seen_at": NOW}}
    latest = {("demo", "read_positions"): dict(result=[], created_at=NOW - timedelta(minutes=4))}
    r = _account_snapshot("demo", latest, worker, NOW)
    assert r["worker_online"] is True and r["broker_state"] == "stale"
    assert r["positions"] == []
    assert r["components"]["read_positions"]["age_seconds"] == 240


def test_completed_read_time_is_authoritative():
    worker = {"demo": {"worker_id": "fixture", "last_seen_at": NOW}}
    latest = {
        ("demo", op): dict(result=result, created_at=NOW - timedelta(minutes=4), completed_at=NOW)
        for op, result in [("read_positions", []), ("read_account_information", {"balance": 1})]
    }
    r = _account_snapshot("demo", latest, worker, NOW)
    assert r["broker_state"] == "fresh"


def test_api_liveness_survives_broker_failure(monkeypatch):
    from app.routes.health import health

    monkeypatch.setattr(
        "app.routes.health.component_health",
        lambda: {"database": "available", "broker": {"status": "disconnected"}},
    )
    result = health()
    assert result.status == "degraded"
    assert result.components["broker"]["status"] == "disconnected"
