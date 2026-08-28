from datetime import UTC, datetime, timedelta

from app.routes.gold_quote import _quote_from_payload


def test_gold_quote_uses_midpoint_and_marks_fresh_quote_live() -> None:
    now = datetime(2026, 8, 28, 4, 30, tzinfo=UTC)
    quote = _quote_from_payload(
        {
            "symbol": "XAUUSD",
            "bid": 3412.40,
            "ask": 3412.80,
            "time": now.isoformat(),
        },
        now=now,
    )

    assert quote.price == 3412.60
    assert quote.bid == 3412.40
    assert quote.ask == 3412.80
    assert quote.available is True
    assert quote.stale is False
    assert quote.source == "Vantage MT5"


def test_gold_quote_marks_old_broker_quote_stale() -> None:
    now = datetime(2026, 8, 28, 4, 30, tzinfo=UTC)
    quote = _quote_from_payload(
        {
            "bid": 3412.40,
            "ask": 3412.80,
            "time": (now - timedelta(seconds=16)).isoformat(),
        },
        now=now,
    )

    assert quote.available is True
    assert quote.stale is True


def test_gold_quote_refuses_partial_price() -> None:
    now = datetime(2026, 8, 28, 4, 30, tzinfo=UTC)
    quote = _quote_from_payload(
        {"bid": 3412.40, "ask": None, "time": now.isoformat()},
        now=now,
    )

    assert quote.price is None
    assert quote.available is False
