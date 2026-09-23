"""Regression guards for canonical broker-account performance truth."""

from datetime import UTC, datetime
from decimal import Decimal
from inspect import getsource
from uuid import UUID

from app.broker_settlement_canonical import CanonicalBrokerSettlementManager
from app.dashboard_today_summary import TodayTradingSummaryService
from app.paper_run_epoch import (
    PAPER_RUN_BASELINE_BALANCE,
    PAPER_RUN_STARTED_AT,
    active_paper_epoch,
)
from app.performance_ledger_canonical import CanonicalPerformanceLedgerService
from app.performance_ledger_day33 import Day33PerformanceWindow
from app.performance_runtime import CanonicalPerformanceRuntimeService


def test_account_sync_uses_complete_time_range_history() -> None:
    source = getsource(CanonicalPerformanceLedgerService.sync_user)
    assert "read_deals_by_time_range" in source
    assert "read_deals_by_position" not in source
    assert "_mark_full_backfill" in source


def test_timeline_repairs_xauusd_pips_from_broker_prices() -> None:
    source = getsource(CanonicalPerformanceLedgerService._timeline_rows)
    assert "UPPER(symbol)='XAUUSD'" in source
    assert "(exit_price-entry_price)/0.1" in source
    assert "(entry_price-exit_price)/0.1" in source
    assert 'item["net_pips"] = repair["repaired_pips"]' in source


def test_today_summary_uses_same_pip_fallback() -> None:
    source = getsource(TodayTradingSummaryService.read)
    assert "effective_pips" in source
    assert "o.exit_price - o.entry_price" in source
    assert "o.entry_price - o.exit_price" in source


def test_today_summary_uses_local_calendar_day_bounds() -> None:
    source = getsource(TodayTradingSummaryService.read)
    assert "local_day_bounds" in source
    assert "timezone_name" in source


def test_flat_account_still_reconciles_broker_history() -> None:
    source = getsource(CanonicalBrokerSettlementManager.poll_once)
    assert "_has_unsettled_mapped_positions" in source
    assert "sync_user" in source
    assert "flat_account_truth_sync_complete" in source


def test_broker_confirmed_tp2_automatically_protects_remaining_trade() -> None:
    source = getsource(CanonicalBrokerSettlementManager._apply_profit_protection_ladder)
    assert 'bool(plan["tp1_hit"]) and bool(plan["tp2_hit"])' in source
    assert "_cancel_pending_for_signal" in source
    assert 'minimum_tp_index=3' in source
    assert 'target="entry"' in source


def test_broker_confirmed_tp3_locks_runner_at_tp2() -> None:
    source = getsource(CanonicalBrokerSettlementManager._apply_profit_protection_ladder)
    assert 'minimum_tp_index=4' in source
    assert 'target=Decimal(str(plan["tp2_price"]))' in source


def test_automatic_profit_protection_never_loosens_existing_stop() -> None:
    source = getsource(CanonicalBrokerSettlementManager._protect_open_for_signal)
    assert '"BUY" in side and desired > current' in source
    assert '"SELL" in side and desired < current' in source
    assert "if not tightens" in source


def test_missing_pending_order_is_not_falsely_marked_cancelled_during_fill_race() -> None:
    source = getsource(CanonicalBrokerSettlementManager._cancel_pending_for_signal)
    assert "if order_id not in broker_orders" in source
    assert "continue" in source


def test_owner_paper_origin_is_immutable_and_not_environment_resettable(monkeypatch) -> None:
    owner = UUID("11111111-1111-4111-8111-111111111111")
    monkeypatch.setenv("SUPER_SIGNALS_DAY28_OWNER_ID", str(owner))
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_RESET_AT", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("SUPER_SIGNALS_PAPER_BASELINE_BALANCE", "999999")

    epoch = active_paper_epoch(owner)
    assert epoch is not None
    assert PAPER_RUN_STARTED_AT == datetime(2026, 8, 30, 21, 0, tzinfo=UTC)
    assert PAPER_RUN_BASELINE_BALANCE == Decimal("1517.23")
    assert epoch.started_at == PAPER_RUN_STARTED_AT
    assert epoch.baseline_balance == Decimal("1517.23")


class _StartDayWindowHarness(CanonicalPerformanceRuntimeService):
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime | None, datetime]] = []

    def _run_start(self, user_id):  # noqa: ANN001, ANN201
        return PAPER_RUN_STARTED_AT

    def _signal_window(self, user_id, key, label, since, now):  # noqa: ANN001, ANN201
        self.calls.append((key, since, now))
        return Day33PerformanceWindow(
            key=key,
            label=label,
            cash_pnl=Decimal("31"),
            return_percent=Decimal("3.10"),
            model_500_pnl=Decimal("0"),
            model_500_return_percent=Decimal("0"),
            closed_trades=1,
            wins=1,
            losses=0,
            breakeven=0,
            open_trades=0,
            win_rate_percent=Decimal("100"),
            net_pips=None,
            mixed_instrument_pips=False,
        )


def test_origin_day_every_window_accumulates_from_same_permanent_start() -> None:
    owner = UUID("11111111-1111-4111-8111-111111111111")
    point = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
    service = _StartDayWindowHarness()
    windows = service.read_windows(owner, now=point)

    assert [item.key for item in windows] == ["today", "7d", "30d", "month", "all"]
    assert service.calls == [
        ("today", PAPER_RUN_STARTED_AT, point),
        ("7d", PAPER_RUN_STARTED_AT, point),
        ("30d", PAPER_RUN_STARTED_AT, point),
        ("month", PAPER_RUN_STARTED_AT, point),
        ("all", PAPER_RUN_STARTED_AT, point),
    ]
    assert all(item.cash_pnl == Decimal("31") for item in windows)
    assert all(item.closed_trades == 1 for item in windows)
    assert all(item.wins == 1 and item.losses == 0 and item.breakeven == 0 for item in windows)


def test_provider_reported_tp2_can_trigger_stale_pending_cleanup() -> None:
    source = getsource(CanonicalBrokerSettlementManager._protection_plans)
    assert "provider_hits" in source
    assert "ph.tp2_hit" in source
    assert "signal_lifecycle_events" in source
