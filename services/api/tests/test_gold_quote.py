from datetime import UTC, datetime, timedelta

from app.routes.gold_quote import _gold_api_quote, _xaus_quote


def test_gold_api_quote_reads_free_spot_price() -> None:
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    quote = _gold_api_quote(
        {
            "symbol": "XAU",
            "currency": "USD",
            "price": 4616.40,
            "updatedAt": now.isoformat().replace("+00:00", "Z"),
        },
        now=now,
    )

    assert quote.price == 4616.40
    assert quote.available is True
    assert quote.stale is False
    assert quote.source == "Gold API"


def test_gold_api_quote_accepts_price_when_timestamp_missing() -> None:
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    quote = _gold_api_quote({"price": 4616.40}, now=now)

    assert quote.available is True
    assert quote.stale is False


def test_gold_api_quote_marks_old_quote_stale() -> None:
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    quote = _gold_api_quote(
        {
            "price": 4616.40,
            "updatedAt": (now - timedelta(seconds=91)).isoformat(),
        },
        now=now,
    )

    assert quote.available is True
    assert quote.stale is True


def test_xaus_fallback_reads_spot_price() -> None:
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    quote = _xaus_quote(
        {
            "spot_usd_oz": 4615.90,
            "updated_at": now.isoformat().replace("+00:00", "Z"),
            "data_state": {"status": "fresh"},
        },
        now=now,
    )

    assert quote.price == 4615.90
    assert quote.available is True
    assert quote.stale is False
    assert quote.source == "XAUS"


def test_xaus_fallback_uses_nested_price() -> None:
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    quote = _xaus_quote(
        {"xau": {"price": 4615.75}, "updated_at": now.isoformat()},
        now=now,
    )

    assert quote.price == 4615.75
    assert quote.available is True
