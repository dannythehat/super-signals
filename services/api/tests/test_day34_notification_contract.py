from datetime import UTC, datetime
from decimal import Decimal

from app.summary_notifications_day34 import Day34SummaryNotificationService
from app.telegram_publisher_day34 import Day34TelegramPublisherManager


def test_live_board_empty_state_is_plain_and_provider_hidden() -> None:
    rendered = Day34TelegramPublisherManager._render_live_board([])

    assert rendered == "📌 SUPER SIGNALS · LIVE TRADES\nOPEN 0 · PENDING 0\n\nNo active trades."
    assert "provider" not in rendered.lower()
    assert "balance" not in rendered.lower()
    assert "account" not in rendered.lower()


def test_live_board_counts_signals_and_shows_remaining_legs() -> None:
    rendered = Day34TelegramPublisherManager._render_live_board(
        [
            {
                "symbol": "XAUUSD",
                "side": "BUY",
                "open_tp_indices": [2, 3],
                "pending_tp_indices": [],
            },
            {
                "symbol": "XAUUSD",
                "side": "SELL",
                "open_tp_indices": [],
                "pending_tp_indices": [1, 2],
            },
        ]
    )

    assert "OPEN 1 · PENDING 1" in rendered
    assert "XAUUSD BUY · TP2/TP3 open" in rendered
    assert "XAUUSD SELL · TP1/TP2 pending" in rendered


def test_summary_uses_only_standardized_500_model_wording() -> None:
    title, body = Day34SummaryNotificationService._render(
        {
            "period_type": "daily",
            "period_start": datetime(2026, 8, 12, tzinfo=UTC),
            "period_end": datetime(2026, 8, 13, tzinfo=UTC),
            "total_trades": 3,
            "wins": 2,
            "losses": 1,
            "breakeven": 0,
            "open_trades": 1,
            "net_pips": Decimal("84"),
            "model_500_pnl": Decimal("7.60"),
            "model_500_return_percent": Decimal("1.52"),
        }
    )

    assert title == "📊 DAILY SUPER SIGNALS SUMMARY"
    assert "Closed positions: 3 · Won: 2 · Lost: 1 · BE: 0" in body
    assert "+84 pips" in body
    assert "$500 example at Recommended 1%: +$7.60 · +1.52%" in body
    assert "wins and losses included" in body
    assert "real" not in body.lower()
    assert "balance" not in body.lower()


def test_loss_summary_is_not_hidden_or_celebrated_as_a_win() -> None:
    title, body = Day34SummaryNotificationService._render(
        {
            "period_type": "weekly",
            "period_start": datetime(2026, 8, 10, tzinfo=UTC),
            "period_end": datetime(2026, 8, 17, tzinfo=UTC),
            "total_trades": 2,
            "wins": 0,
            "losses": 2,
            "breakeven": 0,
            "open_trades": 0,
            "net_pips": Decimal("-25.5"),
            "model_500_pnl": Decimal("-5.00"),
            "model_500_return_percent": Decimal("-1.00"),
        }
    )

    assert title == "📊 WEEKLY SUPER SIGNALS SUMMARY"
    assert "Won: 0 · Lost: 2" in body
    assert "-25.5 pips" in body
    assert "-$5.00" in body
    assert "-1%" in body
