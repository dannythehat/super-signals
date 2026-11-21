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


def test_independent_workers_cannot_borrow_each_others_account_or_progress():
    from app.routes.server_monitor import worker_snapshots

    workers = [
        dict(profile="demo", worker_id=k, last_seen_at=NOW) for k in ("worker-a", "worker-b")
    ]
    rows = [
        dict(
            profile="demo",
            claimed_by="worker-a",
            account_id="a",
            operation=op,
            status="succeeded",
            result=value,
            completed_at=NOW,
            created_at=NOW,
        )
        for op, value in [("read_positions", []), ("read_account_information", {"balance": 10})]
    ]
    result = worker_snapshots(rows, workers, NOW)
    assert result[0]["broker_state"] == "fresh" and result[0]["account_ref"] == "a"
    assert result[1]["broker_state"] == "stale" and result[1]["positions"] is None


def test_failed_latest_read_invalidates_previous_success_and_clock_skew():
    from app.routes.server_monitor import worker_snapshots

    worker = dict(profile="demo", worker_id="worker", last_seen_at=NOW)
    rows = [
        dict(
            profile="demo",
            claimed_by="worker",
            account_id="a",
            operation="read_positions",
            status=status,
            result=[] if status == "succeeded" else None,
            completed_at=at,
            created_at=at,
        )
        for status, at in [("succeeded", NOW - timedelta(seconds=5)), ("failed", NOW)]
    ]
    result = worker_snapshots(rows, [worker], NOW)[0]
    assert result["positions"] is None and result["broker_state"] != "fresh"
    worker["last_seen_at"] = NOW + timedelta(minutes=5)
    assert not worker_snapshots(rows, [worker], NOW)[0]["worker_online"]
