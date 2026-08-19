import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.claudy_market_repository import ClaudyMarketRepository

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture(scope="module")
def phase0_engine():
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL Phase 0-lite schema tests")
    engine = create_engine(DATABASE_URL, future=True)
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


def test_phase0_migration_creates_point_in_time_tables_and_revision_constraints(
    phase0_engine,
) -> None:
    inspector = inspect(phase0_engine)
    tables = set(inspector.get_table_names())
    assert {"market_candles", "market_snapshots", "market_event_observations"} <= tables

    candle_unique = {
        item["name"] for item in inspector.get_unique_constraints("market_candles")
    }
    event_unique = {
        item["name"]
        for item in inspector.get_unique_constraints("market_event_observations")
    }
    snapshot_columns = {
        item["name"] for item in inspector.get_columns("market_snapshots")
    }
    assert "uq_market_candles_revision" in candle_unique
    assert "uq_market_candles_payload" in candle_unique
    assert "uq_market_event_observations_revision" in event_unique
    assert "uq_market_event_observations_payload" in event_unique
    assert "event_observation_ids_json" in snapshot_columns


def test_closed_candle_revisions_are_append_only_and_identical_payload_is_noop(
    phase0_engine,
) -> None:
    factory = sessionmaker(bind=phase0_engine, future=True)
    repository = ClaudyMarketRepository(factory)
    observed_at = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    candle = {
        "symbol": "XAUUSD",
        "timeframe": "5m",
        "open_time_utc": datetime(2026, 8, 15, 5, 20, tzinfo=UTC),
        "broker_open_time": "2026-08-15 08:20:00.000",
        "open": "4350",
        "high": "4360",
        "low": "4340",
        "close": "4355",
        "tick_volume": 100,
        "spread": 20,
        "volume": 4,
        "source": "metaapi",
        "payload_digest": "a" * 64,
        "first_observed_at": observed_at,
    }

    first = repository.store_candle(candle)
    duplicate = repository.store_candle(candle)
    revised_candle = {
        **candle,
        "close": "4356",
        "high": "4361",
        "payload_digest": "b" * 64,
        "first_observed_at": observed_at + timedelta(minutes=1),
    }
    revised = repository.store_candle(revised_candle)

    assert first[1:] == (1, True)
    assert duplicate == (first[0], 1, False)
    assert revised[1:] == (2, True)
    assert revised[0] != first[0]

    with phase0_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT revision_index, payload_digest
                FROM market_candles
                WHERE symbol='XAUUSD'
                  AND timeframe='5m'
                  AND open_time_utc=:open_time
                ORDER BY revision_index
                """
            ),
            {"open_time": candle["open_time_utc"]},
        ).all()
    assert rows == [(1, "a" * 64), (2, "b" * 64)]
    assert repository.latest_candle_ids(symbol="XAUUSD")["5m"] == revised[0]


def test_event_revisions_are_point_in_time_and_identical_payload_is_noop(
    phase0_engine,
) -> None:
    factory = sessionmaker(bind=phase0_engine, future=True)
    repository = ClaudyMarketRepository(factory)
    first_observed = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)
    revised_observed = first_observed + timedelta(minutes=10)

    def digest(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return sha256(raw.encode("utf-8")).hexdigest()

    first_payload = {"headline": "Fed speaker starts"}
    revised_payload = {"headline": "Fed speaker starts", "status": "updated"}

    first = repository.store_event_observation(
        source="test-feed",
        external_id="event-1",
        event_type="speech",
        published_at=first_observed,
        first_observed_at=first_observed,
        headline="Fed speaker starts",
        structured_data_json=json.dumps(first_payload),
        raw_payload_json=json.dumps(first_payload),
        payload_digest=digest(first_payload),
    )
    duplicate = repository.store_event_observation(
        source="test-feed",
        external_id="event-1",
        event_type="speech",
        published_at=first_observed,
        first_observed_at=first_observed + timedelta(minutes=1),
        headline="Fed speaker starts",
        structured_data_json=json.dumps(first_payload),
        raw_payload_json=json.dumps(first_payload),
        payload_digest=digest(first_payload),
    )
    revised = repository.store_event_observation(
        source="test-feed",
        external_id="event-1",
        event_type="speech",
        published_at=first_observed,
        first_observed_at=revised_observed,
        headline="Fed speaker starts",
        structured_data_json=json.dumps(revised_payload),
        raw_payload_json=json.dumps(revised_payload),
        payload_digest=digest(revised_payload),
    )

    assert first[1:] == (1, True)
    assert duplicate == (first[0], 1, False)
    assert revised[1:] == (2, True)
    assert revised[0] != first[0]

    before_revision = repository.event_observation_ids_known_at(
        captured_at=first_observed + timedelta(minutes=5)
    )
    after_revision = repository.event_observation_ids_known_at(
        captured_at=revised_observed + timedelta(minutes=1)
    )
    assert before_revision == [first[0]]
    assert after_revision == [revised[0]]

    with phase0_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT revision_index, payload_digest
                FROM market_event_observations
                WHERE source='test-feed' AND external_id='event-1'
                ORDER BY revision_index
                """
            )
        ).all()
    assert [row[0] for row in rows] == [1, 2]
