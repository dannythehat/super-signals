"""Release checks against deployed ORM schema, only in localhost temporary tables."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import Session
from app.models import LocalBridgeCommand, LocalBridgeWorker
from app.local_bridge_queue import LocalBridgeQueue


@pytest.fixture
def bridge_db():
    value = os.getenv("DATABASE_URL", "")
    if not value:
        pytest.skip("localhost test PostgreSQL not configured")
    url = make_url(value)
    if url.host not in ("localhost", "127.0.0.1") or not (url.database or "").endswith("_test"):
        pytest.skip("refusing non-test database")
    engine = create_engine(url)
    with engine.connect() as connection, connection.begin():
        for model in (LocalBridgeCommand, LocalBridgeWorker):
            sql = str(CreateTable(model.__table__).compile(engine)).replace(
                "CREATE TABLE", "CREATE TEMP TABLE"
            )
            connection.execute(text(sql + " ON COMMIT DROP"))
        session = Session(bind=connection)
        yield session
        session.close()
    engine.dispose()


def test_health_query_uses_real_schema_and_never_borrows_worker_identity(bridge_db, monkeypatch):
    from app.routes.health import component_health

    now = datetime.now(UTC)
    s = bridge_db
    s.add_all(
        [
            LocalBridgeWorker(
                worker_id=w, profile="demo", version="fixture", capabilities=[], last_seen_at=now
            )
            for w in ("a", "b")
        ]
    )
    for op, result in [("read_positions", []), ("read_account_information", {"balance": 1})]:
        s.add(
            LocalBridgeCommand(
                profile="demo",
                account_id="account-a",
                operation=op,
                payload={},
                status="succeeded",
                claimed_by="a",
                result=result,
                completed_at=now,
                created_at=now,
                attempt_count=1,
            )
        )
    s.flush()
    monkeypatch.setenv("SUPER_SIGNALS_BROKER_TRANSPORT", "local_bridge")
    monkeypatch.setenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "demo")
    monkeypatch.setattr("app.routes.health.get_session_factory", lambda: lambda: nullcontext(s))
    health = component_health()
    assert health["database"] == "available"  # no fictitious worker.account_id column
    assert health["broker"]["workers_observed"] == 2 and health["broker"]["workers_fresh"] == 1
    assert health["broker"]["status"] == "degraded"


def test_seven_ambiguous_records_are_unclaimable_and_immutable_on_late_completion(bridge_db):
    s = bridge_db
    now = datetime.now(UTC)
    commands = [
        LocalBridgeCommand(
            profile="demo",
            account_id="account-a",
            operation="trade",
            payload={},
            status="ambiguous",
            claimed_by="a",
            lease_token=uuid4(),
            lease_expires_at=now - timedelta(minutes=2),
            attempt_count=1,
        )
        for _ in range(7)
    ]
    s.add_all(commands)
    s.flush()
    queue = LocalBridgeQueue(lambda: nullcontext(s), profile="demo", account_id="account-a")
    assert queue.claim(worker_id="a", profile="demo") is None
    assert [(c.status, c.attempt_count, c.result) for c in commands] == [("ambiguous", 1, None)] * 7
    for c in commands:
        queue.complete(command_id=c.id, worker_id="a", lease_token=c.lease_token, status="failed")
        assert c.status == "ambiguous" and c.result is None
