"""Regression guards for canonical broker-account performance truth."""

from inspect import getsource

from app.broker_settlement_canonical import CanonicalBrokerSettlementManager
from app.dashboard_today_summary import TodayTradingSummaryService
from app.performance_ledger_canonical import CanonicalPerformanceLedgerService


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
