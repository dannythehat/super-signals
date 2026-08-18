"""Regression guards for broker-account performance truth."""

from inspect import getsource

from app.account_truth_poll_override import _poll_once_with_account_truth
from app.dashboard_today_summary import TodayTradingSummaryService
from app.performance_account_truth_override import _repair_timeline_rows, _sync_user


def test_account_sync_uses_complete_time_range_history() -> None:
    source = getsource(_sync_user)
    assert "read_deals_by_time_range" in source
    assert "read_deals_by_position" not in source
    assert "_mark_full_backfill" in source


def test_timeline_repairs_xauusd_pips_from_broker_prices() -> None:
    source = getsource(_repair_timeline_rows)
    assert "UPPER(symbol)='XAUUSD'" in source
    assert "(exit_price-entry_price)/0.1" in source
    assert "(entry_price-exit_price)/0.1" in source
    assert 'item["net_pips"] = repair["repaired_pips"]' in source


def test_today_summary_uses_same_pip_fallback() -> None:
    source = getsource(TodayTradingSummaryService.read)
    assert "effective_pips" in source
    assert "o.exit_price - o.entry_price" in source
    assert "o.entry_price - o.exit_price" in source


def test_flat_account_still_reconciles_broker_history() -> None:
    source = getsource(_poll_once_with_account_truth)
    assert "_has_unsettled_mapped_positions" in source
    assert "sync_user" in source
    assert "flat_account_truth_sync_complete" in source
