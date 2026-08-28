from datetime import UTC, datetime

from app.routes.gold_quote import _biquote_quote, _gold_api_quote


def test_biquote_quote_reads_live_mid_bid_ask() -> None:
    now = datetime(2026, 8, 28, 5, 30, tzinfo=UTC)
    quote = _biquote_quote(
        {
            "symbol": "XAUUSD",
            "bid": 4616.20,
            "ask": 4616.60,
            "mid": 4616.40,
            "timestamp": now.isoformat().replace("+00:00", "Z"),
            "marketState": "open",
            "stale": False,
            "quoteAgeSeconds": 0,
        },
        now=now,
    )

    assert quote.price == 4616.40
    assert quote.bid == 4616.20
    assert quote.ask == 4616.60
    assert quote.available is True
    assert quote.stale is False
    assert quote.source == "biquote live MT5"


def test_biquote_quote_builds_midpoint_when_mid_missing() -> None:
    now = datetime(2026, 8, 28, 5, 30, tzinfo=UTC)
    quote = _biquote_quote(
        {
            "bid": 4616.20,
            "ask": 4616.60,
            "marketState": "open",
            "stale": False,
            "quoteAgeSeconds": 1,
        },
        now=now,
    )

    assert quote.price == 4616.40
    assert quote.available is True
    assert quote.stale is False


def test_biquote_quote_marks_closed_market_stale() -> None:
    now = datetime(2026, 8, 28, 5, 30, tzinfo=UTC)
    quote = _biquote_quote(
        {
            "mid": 4616.40,
            "marketState": "closed",
            "stale": False,
            "quoteAgeSeconds": 20,
        },
        now=now,
    )

    assert quote.available is True
    assert quote.stale is True


def test_gold_api_fallback_is_delayed_by_definition() -> None:
    now = datetime(2026, 8, 28, 5, 30, tzinfo=UTC)
    quote = _gold_api_quote({"price": 4615.90}, now=now)

    assert quote.price == 4615.90
    assert quote.available is True
    assert quote.stale is True
    assert quote.source == "Gold API fallback"
