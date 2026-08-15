import os
from datetime import UTC, datetime
from hashlib import sha256
import json
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


def test_phase0_migration_creates_point_in_time_tables_and_identity_constraints(
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
    assert "uq_market_candles_identity" in candle_unique
    assert "uq_market_event_observations_revision" in event_unique
    assert "uq_market_event_observations_payload" in event_unique


def test_closed_candle_persistence_is_idempotent(phase0_engine) -> None:
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

    first_id = repository.store_candle(candle)
    second_id = repository.store_candle(candle)

    assert first_id == second_id
    with phase0_engine.connect() as connection:
        count = connection.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM market_candles
                WHERE symbol='XAUUSD'
                  AND timeframe='5m'
                  AND open_time_utc=:open_time
                """
            ),
            {"open_time": candle["open_time_utc"]},
        )
    assert count == 1


def test_event_revisions_append_and_identical_payload_is_noop(phase0_engine) -> None:
    factory = sessionmaker(bind=phase0_engine, future=True)
    repository = ClaudyMarketRepository(factory)
    observed = datetime(2026, 8, 15, 5, 30, tzinfo=UTC)

    def digest(payload: dict[str, object]) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return sha256(raw.encode("utf-8")).hexdigest()

    first_payload = {"headline": "Fed speaker starts"}
    revised_payload = {"headline": "Fed speaker starts", "status": "updated"}

    first = repository.store_event_observation(
        source="test-feed",
        external_id="event-1",
        event_type="speech",
        published_at=observed,
        first_observed_at=observed,
        headline="Fed speaker starts",
        structured_data_json=json.dumps(first_payload),
        raw_payload_json=json.dumps(first_payload),
        payload_digest=digest(first_payload),
    )
    duplicate = repository.store_event_observation(
        source="test-feed",
        external_id="event-1",
        event_type="speech",
        published_at=observed,
        first_observed_at=observed,
        headline="Fed speaker starts",
        structured_data_json=json.dumps(first_payload),
        raw_payload_json=json.dumps(first_payload),
        payload_digest=digest(first_payload),
    )
    revised = repository.store_event_observation(
        source="test-feed",
        external_id="event-1",
        event_type="speech",
        published_at=observed,
        first_observed_at=observed,
        headline="Fed speaker starts",
        structured_data_json=json.dumps(revised_payload),
        raw_payload_json=json.dumps(revised_payload),
        payload_digest=digest(revised_payload),
    )

    assert first[1:] == (1, True)
    assert duplicate == (first[0], 1, False)
    assert revised[1:] == (2, True)
    assert revised[0] != first[0]

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
