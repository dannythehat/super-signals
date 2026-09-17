"""``ManagementReliabilityRuntime``'s scan query, run against real PostgreSQL.

Every other test for this feature used fakes/mocks and never executed the actual SQL --
which is exactly how a raw-text() parameter/cast bug (`:lookback::interval`, which
Postgres's driver can't tell apart from a literal `:lookback` followed by a `::interval`
cast token) shipped to production and silently crashed every sweep for an hour. This
suite exists specifically to catch that class of bug: it inserts real audit_events rows
and asserts the scan query actually runs and returns the right candidates.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.management_reliability_runtime import ManagementReliabilityRuntime

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def engine():
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")
    engine = create_engine(DATABASE_URL, future=True)
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def conn(engine):
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def runtime(conn) -> ManagementReliabilityRuntime:
    session_factory = sessionmaker(bind=conn, future=True, expire_on_commit=False)
    return ManagementReliabilityRuntime(
        session_factory=session_factory,
        dispatcher=None,  # type: ignore[arg-type]
        demo_management=None,
        live_management=None,
        owner_user_id=uuid4(),
    )


def _insert_failure(conn, *, signal_id: UUID, lifecycle_event_id: UUID, revision_index: int) -> None:
    conn.execute(
        text(
            """
            INSERT INTO audit_events (event_type, entity_type, entity_id, payload)
            VALUES (
                'mt5.day28_route_failure', 'signal', :signal_id,
                CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "signal_id": signal_id,
            "payload": (
                '{"decision":"trade_update","lifecycle_event_id":"%s","source_revision_index":%d}'
                % (lifecycle_event_id, revision_index)
            ),
        },
    )


def test_scan_query_actually_executes_and_finds_a_recent_failure(conn, runtime) -> None:
    signal_id, lifecycle_event_id = uuid4(), uuid4()
    _insert_failure(conn, signal_id=signal_id, lifecycle_event_id=lifecycle_event_id, revision_index=0)

    candidates = runtime._find_candidates()

    matches = [c for c in candidates if c["lifecycle_event_id"] == lifecycle_event_id]
    assert len(matches) == 1
    assert matches[0]["signal_id"] == signal_id
    assert matches[0]["failure_count"] == 1


def test_scan_query_counts_repeated_failures_on_the_same_instruction(conn, runtime) -> None:
    signal_id, lifecycle_event_id = uuid4(), uuid4()
    for _ in range(3):
        _insert_failure(conn, signal_id=signal_id, lifecycle_event_id=lifecycle_event_id, revision_index=0)

    candidates = runtime._find_candidates()

    matches = [c for c in candidates if c["lifecycle_event_id"] == lifecycle_event_id]
    assert len(matches) == 1
    assert matches[0]["failure_count"] == 3


def test_scan_query_ignores_a_failure_outside_the_lookback_window(conn, runtime) -> None:
    signal_id, lifecycle_event_id = uuid4(), uuid4()
    conn.execute(
        text(
            """
            INSERT INTO audit_events (event_type, entity_type, entity_id, payload, created_at)
            VALUES (
                'mt5.day28_route_failure', 'signal', :signal_id,
                CAST(:payload AS jsonb), now() - interval '3 hours'
            )
            """
        ),
        {
            "signal_id": signal_id,
            "payload": (
                '{"decision":"trade_update","lifecycle_event_id":"%s","source_revision_index":0}'
                % lifecycle_event_id
            ),
        },
    )

    candidates = runtime._find_candidates()

    assert all(c["lifecycle_event_id"] != lifecycle_event_id for c in candidates)
