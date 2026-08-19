import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.claudy_market_repository import (
    ClaudyMarketRepository,
    _persisted_position_state_json,
)

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]


def _snapshot_for_helper(
    *,
    availability: dict[str, object],
    position_state_json: str,
) -> dict[str, object]:
    return {
        "data_availability_json": json.dumps(availability, sort_keys=True),
        "position_state_json": position_state_json,
    }


def test_successful_empty_position_read_preserves_known_empty_state() -> None:
    snapshot = _snapshot_for_helper(
        availability={"positions": "available"},
        position_state_json="[]",
    )
    assert _persisted_position_state_json(snapshot) == "[]"


def test_successful_populated_position_read_preserves_sanitized_list() -> None:
    positions = json.dumps(
        [{"id": "position-1", "symbol": "XAUUSD"}],
        sort_keys=True,
    )
    snapshot = _snapshot_for_helper(
        availability={"positions": "available"},
        position_state_json=positions,
    )
    assert _persisted_position_state_json(snapshot) == positions


def test_failed_position_read_becomes_unknown_on_persistence() -> None:
    snapshot = _snapshot_for_helper(
        availability={"positions": "metaapi_timeout"},
        position_state_json="[]",
    )
    assert _persisted_position_state_json(snapshot) is None


def test_pre_position_snapshot_is_unknown_when_never_attempted() -> None:
    snapshot = _snapshot_for_helper(
        availability={"reference_demo_account": "not_configured"},
        position_state_json="[]",
    )
    assert _persisted_position_state_json(snapshot) is None


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture(scope="module")
def phase0_engine():
    if not DATABASE_URL:
        pytest.skip(
            "DATABASE_URL is required for PostgreSQL Phase 0-lite schema tests"
        )
    engine = create_engine(DATABASE_URL, future=True)
    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    yield engine
    command.downgrade(config, "base")
    engine.dispose()


def test_position_state_column_is_nullable(phase0_engine) -> None:
    columns = {
        item["name"]: item
        for item in inspect(phase0_engine).get_columns("market_snapshots")
    }
    assert columns["position_state_json"]["nullable"] is True


def test_repository_persists_known_empty_populated_and_unknown_states(
    phase0_engine,
) -> None:
    factory = sessionmaker(bind=phase0_engine, future=True)
    repository = ClaudyMarketRepository(factory)
    captured_at = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)

    def snapshot(
        *,
        positions: str,
        availability: dict[str, object],
        digest: str,
    ) -> dict[str, object]:
        return {
            "captured_at": captured_at,
            "symbol": "XAUUSD",
            "capture_status": "partial",
            "bid": None,
            "ask": None,
            "mid": None,
            "spread": None,
            "quote_time": None,
            "quote_age_seconds": None,
            "session_code": "off_hours",
            "terminal_trade_allowed": None,
            "position_state_json": positions,
            "order_state_json": None,
            "cross_market_state_json": "{}",
            "provider_state_json": "{}",
            "claudy_state_json": "{}",
            "data_availability_json": json.dumps(availability, sort_keys=True),
            "event_observation_ids_json": "[]",
            "latest_m1_id": None,
            "latest_m5_id": None,
            "latest_m15_id": None,
            "latest_h1_id": None,
            "latest_h4_id": None,
            "latest_d1_id": None,
            "snapshot_digest": digest,
        }

    known_empty_id = repository.store_snapshot(
        snapshot(
            positions="[]",
            availability={"positions": "available"},
            digest="a" * 64,
        )
    )
    populated_id = repository.store_snapshot(
        snapshot(
            positions='[{"id":"position-1","symbol":"XAUUSD"}]',
            availability={"positions": "available"},
            digest="b" * 64,
        )
    )
    failed_id = repository.store_snapshot(
        snapshot(
            positions="[]",
            availability={"positions": "metaapi_timeout"},
            digest="c" * 64,
        )
    )
    not_attempted_id = repository.store_snapshot(
        snapshot(
            positions="[]",
            availability={"reference_demo_account": "not_configured"},
            digest="d" * 64,
        )
    )

    with phase0_engine.connect() as connection:
        rows = {
            row.id: row.position_state_json
            for row in connection.execute(
                text(
                    """
                    SELECT id, position_state_json
                    FROM market_snapshots
                    WHERE id IN (
                        :known_empty_id,
                        :populated_id,
                        :failed_id,
                        :not_attempted_id
                    )
                    """
                ),
                {
                    "known_empty_id": known_empty_id,
                    "populated_id": populated_id,
                    "failed_id": failed_id,
                    "not_attempted_id": not_attempted_id,
                },
            ).all()
        }

    assert rows[known_empty_id] == []
    assert rows[populated_id] == [
        {"id": "position-1", "symbol": "XAUUSD"}
    ]
    assert rows[failed_id] is None
    assert rows[not_attempted_id] is None
