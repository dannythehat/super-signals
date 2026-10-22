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
