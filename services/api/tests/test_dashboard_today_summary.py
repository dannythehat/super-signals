import inspect
from datetime import UTC, datetime

from app.dashboard_today_summary import TodayTradingSummary, local_day_bounds
from app.routes.dashboard_day32 import (
    TodayTradingSummaryResponse,
    _active_broker_order_ids,
    _broker_history_proves_terminal,
    _broker_pending_trade_count,
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


def test_today_pending_is_unknown_when_broker_truth_is_unavailable() -> None:
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


def test_empty_mt5_active_order_list_means_zero_pending_trades() -> None:
    assert _broker_pending_trade_count(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        session_started_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
        active_order_ids=set(),
    ) == 0


def test_missing_mt5_active_order_read_never_falls_back_to_local_pending_rows() -> None:
    assert _broker_pending_trade_count(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        session_started_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
        active_order_ids=None,
    ) is None


def test_broker_history_terminal_ticket_overrides_stale_current_order_replica() -> None:
    order_id = "123456"
    history = [
        {
            "id": order_id,
            "state": "ORDER_STATE_FILLED",
            "doneTime": "2026-08-20T10:00:00.000Z",
        }
    ]
    assert _broker_history_proves_terminal(order_id, history) is True


def test_unfinished_matching_ticket_is_not_terminal() -> None:
    order_id = "123456"
    history = [{"id": order_id, "state": "ORDER_STATE_PLACED", "doneTime": None}]
    assert _broker_history_proves_terminal(order_id, history) is False


def test_other_ticket_history_cannot_terminalize_current_pending_order() -> None:
    history = [
        {
            "id": "different-ticket",
            "state": "ORDER_STATE_FILLED",
            "doneTime": "2026-08-20T10:00:00.000Z",
        }
    ]
    assert _broker_history_proves_terminal("123456", history) is False


def test_dashboard_pending_count_requires_current_canonical_pending_status() -> None:
    source = inspect.getsource(_broker_pending_trade_count)
    assert "p.status='pending'" in source


def test_dashboard_pending_broker_verification_uses_one_bulk_history_read() -> None:
    source = inspect.getsource(_active_broker_order_ids)
    assert "read_history_orders_by_time_range" in source
    assert "read_history_orders_by_ticket" not in source
    assert "for item in orders:" not in source


def test_today_summary_has_no_local_pending_field_or_fallback() -> None:
    assert "pending" not in TodayTradingSummary.__dataclass_fields__
