import inspect
from datetime import UTC, datetime

from app.dashboard_today_summary import TodayTradingSummary, local_day_bounds
from app.routes.dashboard_day32 import (
    TodayTradingSummaryResponse,
    _local_pending_trade_count,
    account_dashboard_today,
)


def test_sofia_today_uses_local_calendar_day_not_utc_day() -> None:
    timezone, start, end = local_day_bounds(
        "Europe/Sofia",
        now_utc=datetime(2026, 8, 18, 14, 0, tzinfo=UTC),
    )
    assert timezone == "Europe/Sofia"
    assert start == datetime(2026, 8, 17, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 18, 21, 0, tzinfo=UTC)


def test_invalid_browser_timezone_falls_back_safely_to_utc() -> None:
    timezone, start, end = local_day_bounds(
        "not/a-real-zone",
        now_utc=datetime(2026, 8, 18, 14, 0, tzinfo=UTC),
    )
    assert timezone == "UTC"
    assert start == datetime(2026, 8, 18, 0, 0, tzinfo=UTC)
    assert end == datetime(2026, 8, 19, 0, 0, tzinfo=UTC)


def test_today_summary_contract_counts_signals_not_tp_positions() -> None:
    response = TodayTradingSummaryResponse(
        timezone="Europe/Sofia",
        trades=7,
        wins=4,
        losses=3,
        breakeven=0,
        open=0,
        pending=2,
        settling=0,
        realised_pnl=5.63,
        winning_pips=349.3,
        net_pips=56.3,
    )
    payload = response.model_dump(mode="json")
    assert payload["trades"] == 7
    assert payload["wins"] + payload["losses"] + payload["breakeven"] + payload["open"] + payload["settling"] == 7
    assert payload["pending"] == 2
    assert payload["realised_pnl"] == 5.63
    assert payload["winning_pips"] == 349.3
    assert payload["net_pips"] == 56.3
    assert payload["broker_trade_action_created"] is False
    assert "balance_adjustment" not in payload
    assert "reconciliation_ready" not in payload
    assert "reconciled" not in payload
    assert "provider" not in payload
    assert "source_id" not in payload


def test_today_pending_can_be_unknown_without_breaking_contract() -> None:
    response = TodayTradingSummaryResponse(
        timezone="Europe/Sofia",
        trades=0,
        wins=0,
        losses=0,
        breakeven=0,
        open=0,
        pending=None,
        settling=0,
        realised_pnl=0,
        winning_pips=0,
        net_pips=0,
    )
    assert response.pending is None


def test_dashboard_pending_count_uses_reconciled_local_pending_ledger() -> None:
    source = inspect.getsource(_local_pending_trade_count)
    assert "p.status='pending'" in source
    assert "p.broker_order_id IS NOT NULL" in source
    assert "src.status<>'revoked'" in source


def test_today_summary_never_contacts_metaapi() -> None:
    source = inspect.getsource(account_dashboard_today)
    assert "read_orders" not in source
    assert "read_history_orders_by_time_range" not in source
    assert "read_history_orders_by_ticket" not in source
    assert "_active_broker_order_ids" not in source


def test_today_summary_has_no_local_pending_field_or_fallback() -> None:
    assert "pending" not in TodayTradingSummary.__dataclass_fields__
