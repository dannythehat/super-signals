"""Day 33 focused acceptance coverage for attribution, colours and privacy."""

from datetime import UTC, datetime
from uuid import UUID

from app.performance_ledger_day33 import (
    Day33PerformanceLedgerService,
    stable_color_index,
    trader_stream_for,
)
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2

SOURCE_ID = UUID("11111111-1111-1111-1111-111111111111")
SIGNAL_ID = UUID("22222222-2222-2222-2222-222222222222")


def test_trader_stream_attribution_is_conservative() -> None:
    assert trader_stream_for("Matthew trades", "BUY GOLD @ 4400") == "Matthew"
    assert trader_stream_for("Jeff Signals", "SELL GOLD @ 4400") == "Jeff"
    assert (
        trader_stream_for(
            "TDC V2 💎 (NEW)",
            "BUY LIMITS GOLD @ 4400-4398 HIGH RISK TRADE",
        )
        == "Matthew"
    )
    assert trader_stream_for("TDC V2 💎 (NEW)", "BUY GOLD @ 4400") is None


def test_source_colour_is_stable_and_trader_specific() -> None:
    first = stable_color_index(SOURCE_ID, None)
    assert first == stable_color_index(SOURCE_ID, None)
    assert 0 <= first <= 7
    assert stable_color_index(SOURCE_ID, "Matthew") == stable_color_index(
        SOURCE_ID, "Matthew"
    )


def _executed_row() -> dict[str, object]:
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    return {
        "signal_id": SIGNAL_ID,
        "symbol": "XAUUSD",
        "side": "BUY",
        "source_id": SOURCE_ID,
        "source_label": "Private Provider",
        "original_text": "BUY LIMITS GOLD @ 4400-4398 HIGH RISK TRADE",
        "opened_at": now,
        "closed_at": now,
        "position_count": 1,
        "open_positions": 0,
        "pending_positions": 0,
        "closed_positions": 1,
        "cash_pnl": 10,
        "net_pips": 20,
        "model_500_pnl": 5,
        "trader_stream": "Matthew",
        "close_reason": "take_profit",
        "has_win": True,
        "has_loss": False,
        "has_breakeven": False,
        "has_unknown": False,
        "status_override": None,
    }


def test_provider_identity_is_hidden_from_invited_user() -> None:
    admin = Day33PerformanceLedgerService._timeline_trade(
        _executed_row(), provider_visible=True
    )
    invited = Day33PerformanceLedgerService._timeline_trade(
        _executed_row(), provider_visible=False
    )
    assert admin.status == "won"
    assert admin.status_color == "green"
    assert admin.source_label == "Private Provider"
    assert admin.trader_stream == "Matthew"
    assert admin.source_color_index is not None
    assert invited.source_label is None
    assert invited.trader_stream is None
    assert invited.source_color_index is None


def test_skipped_trade_is_amber_and_provider_hidden_for_invited_user() -> None:
    row = _executed_row()
    row.update(
        {
            "status_override": "skipped",
            "position_count": 0,
            "closed_positions": 0,
            "cash_pnl": None,
            "net_pips": None,
            "model_500_pnl": None,
            "trader_stream": None,
            "close_reason": "entry_price_unavailable",
        }
    )
    admin = Day33PerformanceLedgerServiceV2._timeline_trade(
        row, provider_visible=True
    )
    invited = Day33PerformanceLedgerServiceV2._timeline_trade(
        row, provider_visible=False
    )
    assert admin.status == "skipped"
    assert admin.status_color == "amber"
    assert admin.status_label == "Skipped"
    assert admin.trader_stream == "Matthew"
    assert invited.source_label is None
    assert invited.trader_stream is None
    assert invited.close_reason == "entry_price_unavailable"
