"""Real-Postgres proof that a truncated broker deal-history fetch is never silent.

`CanonicalPerformanceLedgerService.sync_user` pages through MetaAPI's deal history with
`read_deals_by_time_range`. If a page call raises `MetaApiGatewayError` with code
`metaapi_terminal_data_unavailable`, the original code stored whatever partial pages it
already had and moved on -- with no record that the fetch never reached `captured_at`.
The next sync's start time is derived from `MAX(occurred_at)` of whatever we already have
stored, so it silently trusted the partial watermark and never went back for the deals
between where the partial page ended and where the fetch was cut off.

This is not hypothetical: cross-checking the Owner account's own broker balance snapshots
against `broker_deals` for two real trading days (Fri 25 Sep, Mon 28 Sep 2026) showed
$169.83 and $55.67 of real, broker-confirmed balance movement with no matching deal record
at all -- money that was really lost but is invisible to every provider-level P&L query.

These tests prove the recovery mechanism directly against real Postgres: a truncated sync
is recorded, `_history_start` re-widens the next attempt back over the gap instead of
trusting the partial watermark, and a later clean sync resolves it so it stops widening.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.metaapi_gateway import MetaApiGatewayError
from app.performance_ledger_canonical import (
    _CHECKPOINT_EVENT,
    _HISTORY_OVERLAP,
    _KNOWN_DEAL_GAP_WINDOWS,
    CanonicalPerformanceLedgerService,
)
from app.seed import seed_owner

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@pytest.fixture()
def db(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(DATABASE_URL, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with Session(engine) as session:
        owner = seed_owner(session, "owner@example.com", "Danny")
        mt5_account_id = session.execute(
            text(
                """
                INSERT INTO mt5_accounts (
                    id, owner_user_id, account_environment, login, server,
                    metaapi_account_id, metaapi_token_ciphertext, metaapi_token_fingerprint,
                    status
                ) VALUES (
                    :id, :owner_id, 'live', '12345', 'Vantage-Live',
                    'meta-acct-1', '\\x00'::bytea, 'fingerprint', 'connected'
                )
                RETURNING id
                """
            ),
            {"id": uuid4(), "owner_id": owner.id},
        ).scalar_one()
        session.commit()
        owner_id = owner.id

    yield session_factory, owner_id, mt5_account_id

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


class _Harness(CanonicalPerformanceLedgerService):
    """Exercises the ledger service's private methods against a real session factory
    without needing the full MetaAPI-gateway/cipher constructor dependencies."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory


def _insert_deal(
    session: Session, *, user_id, mt5_account_id, occurred_at: datetime, profit: str
) -> None:
    session.execute(
        text(
            """
            INSERT INTO broker_deals (
                id, user_id, mt5_account_id, broker_deal_id, deal_type, entry_type,
                symbol, volume, price, profit, commission, swap, occurred_at,
                raw_payload
            ) VALUES (
                :id, :user_id, :mt5_account_id, :deal_id, 'DEAL_TYPE_BUY',
                'DEAL_ENTRY_OUT', 'XAUUSD', 0.01, 4200.00, :profit, 0, 0,
                :occurred_at, '{}'::jsonb
            )
            """
        ),
        {
            "id": uuid4(),
            "user_id": user_id,
            "mt5_account_id": mt5_account_id,
            "deal_id": uuid4().hex,
            "profit": profit,
            "occurred_at": occurred_at,
        },
    )


def _insert_checkpoint(
    session: Session, *, user_id, mt5_account_id, created_at: datetime
) -> None:
    session.execute(
        text(
            """
            INSERT INTO audit_events (
                actor_user_id, event_type, entity_type, entity_id, payload, created_at
            )
            VALUES (:user_id, :event_type, 'mt5_account', :account_id, '{}'::jsonb, :created_at)
            """
        ),
        {
            "user_id": user_id,
            "event_type": _CHECKPOINT_EVENT,
            "account_id": mt5_account_id,
            "created_at": created_at,
        },
    )


def test_history_start_uses_normal_watermark_when_no_gap_is_outstanding(db) -> None:
    session_factory, owner_id, mt5_account_id = db
    latest_deal_at = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        _insert_checkpoint(
            session, user_id=owner_id, mt5_account_id=mt5_account_id, created_at=latest_deal_at
        )
        _insert_deal(
            session,
            user_id=owner_id,
            mt5_account_id=mt5_account_id,
            occurred_at=latest_deal_at,
            profit="1.00",
        )
        session.commit()

    harness = _Harness(session_factory)
    start_time, full_backfill = CanonicalPerformanceLedgerService._history_start(
        harness, owner_id, mt5_account_id
    )

    assert full_backfill is False
    assert start_time == latest_deal_at - _HISTORY_OVERLAP


def test_truncated_sync_widens_next_history_start_back_over_the_gap(db) -> None:
    """The real shape of Friday's $169.83 gap: a sync's fetch gets cut short mid-page,
    the partial deals it did get are stored, and the watermark they leave behind is much
    later than where the fetch actually started. Recording the truncation must make the
    next `_history_start` call go back to the original request, not trust that watermark."""
    session_factory, owner_id, mt5_account_id = db
    requested_start = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)
    requested_end = datetime(2026, 9, 25, 1, 15, tzinfo=UTC)
    partial_deal_at = datetime(2026, 9, 25, 1, 12, tzinfo=UTC)

    with session_factory() as session:
        _insert_checkpoint(
            session, user_id=owner_id, mt5_account_id=mt5_account_id, created_at=requested_start
        )
        _insert_deal(
            session,
            user_id=owner_id,
            mt5_account_id=mt5_account_id,
            occurred_at=partial_deal_at,
            profit="-14.82",
        )
        session.commit()

    harness = _Harness(session_factory)

    # Without a recorded truncation, the watermark alone would trust the partial deal.
    naive_start, _ = CanonicalPerformanceLedgerService._history_start(
        harness, owner_id, mt5_account_id
    )
    assert naive_start == partial_deal_at - _HISTORY_OVERLAP

    CanonicalPerformanceLedgerService._record_partial_sync_truncation(
        harness,
        user_id=owner_id,
        mt5_account_id=mt5_account_id,
        requested_start_time=requested_start,
        requested_end_time=requested_end,
        payloads_fetched=3,
        offset_reached=0,
        error_code="metaapi_terminal_data_unavailable",
    )

    outstanding = CanonicalPerformanceLedgerService._outstanding_partial_sync_start(
        harness, owner_id, mt5_account_id
    )
    assert outstanding == requested_start

    recovered_start, full_backfill = CanonicalPerformanceLedgerService._history_start(
        harness, owner_id, mt5_account_id
    )
    assert full_backfill is False
    assert recovered_start == requested_start
    assert recovered_start < naive_start


def test_resolved_sync_stops_widening_history_start(db) -> None:
    session_factory, owner_id, mt5_account_id = db
    requested_start = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)
    latest_deal_at = datetime(2026, 9, 25, 2, 0, tzinfo=UTC)

    with session_factory() as session:
        _insert_checkpoint(
            session, user_id=owner_id, mt5_account_id=mt5_account_id, created_at=requested_start
        )
        _insert_deal(
            session,
            user_id=owner_id,
            mt5_account_id=mt5_account_id,
            occurred_at=latest_deal_at,
            profit="-14.82",
        )
        session.commit()

    harness = _Harness(session_factory)
    CanonicalPerformanceLedgerService._record_partial_sync_truncation(
        harness,
        user_id=owner_id,
        mt5_account_id=mt5_account_id,
        requested_start_time=requested_start,
        requested_end_time=latest_deal_at,
        payloads_fetched=1,
        offset_reached=0,
        error_code="metaapi_terminal_data_unavailable",
    )
    assert (
        CanonicalPerformanceLedgerService._outstanding_partial_sync_start(
            harness, owner_id, mt5_account_id
        )
        is not None
    )

    CanonicalPerformanceLedgerService._record_partial_sync_resolved(
        harness, user_id=owner_id, mt5_account_id=mt5_account_id
    )

    assert (
        CanonicalPerformanceLedgerService._outstanding_partial_sync_start(
            harness, owner_id, mt5_account_id
        )
        is None
    )
    start_time, _ = CanonicalPerformanceLedgerService._history_start(
        harness, owner_id, mt5_account_id
    )
    assert start_time == latest_deal_at - _HISTORY_OVERLAP


def test_outstanding_partial_sync_start_is_none_by_default(db) -> None:
    session_factory, owner_id, mt5_account_id = db
    harness = _Harness(session_factory)
    assert (
        CanonicalPerformanceLedgerService._outstanding_partial_sync_start(
            harness, owner_id, mt5_account_id
        )
        is None
    )


class _FakeGateway:
    """Returns one canned deal per known gap window, keyed by its exact start_time, and
    counts how many times each window was actually fetched."""

    def __init__(self, *, fail_window_index: int | None = None) -> None:
        self.calls: list[datetime] = []
        self._fail_window_index = fail_window_index

    async def read_deals_by_time_range(
        self, *, token, account_id, region, start_time, end_time, offset, limit
    ):
        self.calls.append(start_time)
        if offset > 0:
            return []
        for index, (_, window_start, _) in enumerate(_KNOWN_DEAL_GAP_WINDOWS):
            if start_time == window_start:
                if index == self._fail_window_index:
                    raise MetaApiGatewayError("metaapi_terminal_data_unavailable")
                return [
                    {
                        "id": f"gap-deal-{index}",
                        "type": "DEAL_TYPE_BUY",
                        "entryType": "DEAL_ENTRY_OUT",
                        "time": (window_start + timedelta(hours=1)).isoformat(),
                        "symbol": "XAUUSD",
                        "volume": 0.01,
                        "price": 4200.0,
                        "profit": -50.0,
                        "commission": 0,
                        "swap": 0,
                    }
                ]
        return []


class _GapRepairHarness(_Harness):
    def __init__(self, session_factory, gateway) -> None:
        super().__init__(session_factory)
        self._gateway = gateway


async def _run_repair(harness) -> int:
    return await CanonicalPerformanceLedgerService._repair_known_deal_gaps_safely(
        harness,
        user_id=harness.owner_id,
        mt5_account_id=harness.mt5_account_id,
        token="tok",
        account_id="acct",
        region="region",
    )


@pytest.mark.asyncio
async def test_known_gap_windows_are_repaired_once_and_deals_are_stored(db) -> None:
    session_factory, owner_id, mt5_account_id = db
    gateway = _FakeGateway()
    harness = _GapRepairHarness(session_factory, gateway)
    harness.owner_id = owner_id
    harness.mt5_account_id = mt5_account_id

    added_first = await _run_repair(harness)
    assert added_first == len(_KNOWN_DEAL_GAP_WINDOWS)

    with session_factory() as session:
        stored = session.execute(
            text("SELECT broker_deal_id FROM broker_deals WHERE user_id=:u"),
            {"u": owner_id},
        ).scalars().all()
    assert set(stored) == {f"gap-deal-{i}" for i in range(len(_KNOWN_DEAL_GAP_WINDOWS))}

    calls_after_first = len(gateway.calls)

    added_second = await _run_repair(harness)
    assert added_second == 0
    assert len(gateway.calls) == calls_after_first, "already-repaired windows must not refetch"


@pytest.mark.asyncio
async def test_a_failed_window_is_not_marked_repaired_and_retries_next_time(db) -> None:
    session_factory, owner_id, mt5_account_id = db
    gateway = _FakeGateway(fail_window_index=0)
    harness = _GapRepairHarness(session_factory, gateway)
    harness.owner_id = owner_id
    harness.mt5_account_id = mt5_account_id

    added = await _run_repair(harness)
    # Window 0 failed (no deal stored for it); window 1 still succeeded cleanly.
    assert added == 1

    with session_factory() as session:
        stored = session.execute(
            text("SELECT broker_deal_id FROM broker_deals WHERE user_id=:u"),
            {"u": owner_id},
        ).scalars().all()
    assert set(stored) == {"gap-deal-1"}

    repaired = CanonicalPerformanceLedgerService._repaired_deal_gap_windows(
        harness, owner_id, mt5_account_id
    )
    assert _KNOWN_DEAL_GAP_WINDOWS[0][0] not in repaired
    assert _KNOWN_DEAL_GAP_WINDOWS[1][0] in repaired

    # A follow-up call must retry only the still-failed window.
    gateway._fail_window_index = None
    added_retry = await _run_repair(harness)
    assert added_retry == 1
    with session_factory() as session:
        stored = session.execute(
            text("SELECT broker_deal_id FROM broker_deals WHERE user_id=:u"),
            {"u": owner_id},
        ).scalars().all()
    assert set(stored) == {"gap-deal-0", "gap-deal-1"}
