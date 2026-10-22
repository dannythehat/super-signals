"""22 October: late broker trades must not masquerade as today's results."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.superseded_pending_guard import (
    SupersededPendingOrderGuard,
    SupersededPendingResult,
)
from app.telegram_publisher_canonical import (
    CanonicalTelegramPublisherManager,
    _broker_settlement_publication_cutoff,
)


def test_late_october_20_settlements_are_excluded_october_22() -> None:
    now = datetime(2026, 10, 22, 2, 46, tzinfo=UTC)
    cutoff = _broker_settlement_publication_cutoff(now)
    assert cutoff == datetime(2026, 10, 22, 2, 16, tzinfo=UTC)
    assert datetime(2026, 10, 20, 10, 37, tzinfo=UTC) < cutoff


def test_reporting_day_rollover_overrides_freshness_window() -> None:
    # 21:02 Sofia is already the next reporting day. A 20:59 close is old.
    now = datetime(2026, 10, 21, 18, 2, tzinfo=UTC)
    cutoff = _broker_settlement_publication_cutoff(now)
    assert cutoff == datetime(2026, 10, 21, 18, tzinfo=UTC)
    assert datetime(2026, 10, 21, 17, 59, tzinfo=UTC) < cutoff


def test_summertime_transition_uses_sofia_not_fixed_utc_offset() -> None:
    # After Bulgaria's October DST shift, 21:00 local is 19:00 UTC.
    local = datetime(2026, 10, 26, 21, 3, tzinfo=ZoneInfo("Europe/Sofia"))
    cutoff = _broker_settlement_publication_cutoff(local.astimezone(UTC))
    assert cutoff == datetime(2026, 10, 26, 19, tzinfo=UTC)


def test_outbox_suppresses_late_broker_events_at_seed_and_claim() -> None:
    seed = inspect.getsource(CanonicalTelegramPublisherManager._seed_missing_publications)
    claim = inspect.getsource(CanonicalTelegramPublisherManager._claim_lifecycle)
    assert "ev.occurred_at<:settlement_cutoff" in seed
    assert "ev.occurred_at>=:settlement_cutoff" in seed
    assert "ev.occurred_at>=:settlement_cutoff" in claim
    assert "ev.event_type<>'broker_position_settled'" in claim
    assert "AND root.status='sent'" in claim


@pytest.mark.asyncio
async def test_periodic_pending_cleanup_is_throttled_but_not_execution_guard() -> None:
    guard = object.__new__(SupersededPendingOrderGuard)
    guard._cleanup_cache = {}
    calls = {"rows": 0, "cancel": 0}

    def rows(**_kwargs):
        calls["rows"] += 1
        return [{"broker_order_id": "old-pending"}]

    async def cancel_rows(**_kwargs):
        calls["cancel"] += 1
        return SupersededPendingResult(1, 0, 0, 1)

    guard._all_superseded_rows = rows
    guard._cancel_rows = cancel_rows
    user = uuid4()

    first = await guard.cleanup_existing(user_id=user, account_environment="demo")
    second = await guard.cleanup_existing(user_id=user, account_environment="demo")
    assert first.unresolved == second.unresolved == 1
    assert calls == {"rows": 1, "cancel": 1}
    assert "cancel_before_signal" in inspect.getsource(SupersededPendingOrderGuard)
    assert "superseded_pending_not_proven_safe" in inspect.getsource(
        SupersededPendingOrderGuard.cancel_before_signal
    )


def _stale_ticket() -> tuple[dict[str, object], dict[str, object]]:
    row = {
        "broker_order_id": "2218888267",
        "broker_client_id": "SS_63b994fc24ed_E2T2",
        "symbol": "XAUUSD",
    }
    history = {
        "id": "2218888267",
        "clientId": "SS_63b994fc24ed_E2T2[cancelOrde",
        "state": "ORDER_STATE_PLACED",
        "symbol": "XAUUSD",
        "type": "ORDER_TYPE_BUY_LIMIT",
        "currentVolume": 0.05,
    }
    return row, history


def test_cancelled_mt5_ticket_matches_truncated_annotation_not_arbitrary_id() -> None:
    row, historical = _stale_ticket()
    assert SupersededPendingOrderGuard._matching_history(row, [historical]) == historical
    assert SupersededPendingOrderGuard._matching_history(
        row, [{**historical, "id": "unrelated"}]
    ) is None


def test_stale_pending_cancel_reconciles_only_with_multiple_broker_checks() -> None:
    row, historical = _stale_ticket()

    def confirmed(orders=None, positions=None, record=None):
        return SupersededPendingOrderGuard._confirmed_cancelled_history(
            row=row,
            terminal=record or historical,
            active_order_ids=set(orders or []),
            broker_positions=positions or [],
        )

    assert confirmed()
    assert not confirmed(orders=["2218888267"])
    assert not confirmed(positions=[{"orderId": "2218888267"}])
    assert not confirmed(positions=[{"clientId": row["broker_client_id"]}])
    assert not confirmed(record={**historical, "clientId": row["broker_client_id"]})
    assert not confirmed(record={**historical, "state": "ORDER_STATE_FILLED"})
    assert not confirmed(record={**historical, "symbol": "EURUSD"})
    assert not confirmed(record={**historical, "positionId": "some-filled-position"})


def test_no_fill_guard_remains_before_terminalisation() -> None:
    source = inspect.getsource(SupersededPendingOrderGuard._cancel_rows)
    assert "_confirmed_cancelled_history(" in source
    assert "not self._known_broker_deal_for_order" in source
    assert "self._persist_cancelled(user_id, row, broker_mutation=False)" in source


def test_tp_publications_require_older_confirmed_tp_to_be_sent_first() -> None:
    claim = inspect.getsource(CanonicalTelegramPublisherManager._claim_lifecycle)
    assert "AND preceding.status='sent'" in claim
    assert "earlier.tp_index<event_position.tp_index" in claim
    assert "ORDER BY ev.occurred_at,event_position.tp_index" in claim


def test_tp_balance_is_not_current_equity_snapshot_copied_to_past_events() -> None:
    formatter = inspect.getsource(
        CanonicalTelegramPublisherManager._settlement_financial_lines
    )
    assert "cash_change != event_cash" in formatter
    assert "Broker snapshot confirming" in formatter
    claim = inspect.getsource(CanonicalTelegramPublisherManager._claim_lifecycle)
    assert "self._settlement_financial_lines(" in claim


def test_member_number_allocator_never_waits_for_another_publisher() -> None:
    class NoLockSession:
        def __init__(self) -> None:
            self.calls = []

        def execute(self, statement, _params=None):
            self.calls.append(str(statement))
            return self

        def scalar_one(self):
            return False

    session = NoLockSession()
    CanonicalTelegramPublisherManager._assign_member_trade_numbers(
        session, fresh_after=datetime.now(UTC)
    )
    assert len(session.calls) == 1
    assert "pg_try_advisory_xact_lock" in session.calls[0]
    source = inspect.getsource(
        CanonicalTelegramPublisherManager._assign_member_trade_numbers
    )
    assert "FROM positions AS p" in source
    assert "SELECT sig.id" in source
    assert "WITH relevant AS" in source


def test_startup_telegram_seed_and_identity_repairs_do_not_block_readiness() -> None:
    from pathlib import Path

    main = Path("services/api/app/main.py").read_text(encoding="utf-8")
    assert "startup_telegram_task = asyncio.create_task(" in main
    assert "await asyncio.to_thread(publisher._seed_missing_publications)" in main
    startup = main[main.index("    await publisher.start()"):main.index(
        "    if stale_position_watchdog is not None:"
    )]
    assert "await _seed_telegram_at_startup_safely()" not in startup
