from datetime import UTC, datetime, timedelta

from app.routes.gold_quote import _quote_from_payload


def test_gold_quote_reads_free_spot_price_and_marks_fresh_quote_live() -> None:
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    quote = _quote_from_payload(
        {
            "symbol": "XAU",
            "currency": "USD",
            "price": 4616.40,
            "updatedAt": now.isoformat().replace("+00:00", "Z"),
        },
        now=now,
    )

    assert quote.price == 4616.40
    assert quote.bid is None
    assert quote.ask is None
    assert quote.available is True
    assert quote.stale is False
    assert quote.source == "Gold API"


def test_gold_quote_marks_old_public_quote_stale() -> None:
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    quote = _quote_from_payload(
        {
            "price": 4616.40,
            "updatedAt": (now - timedelta(seconds=61)).isoformat(),
        },
        now=now,
    )

    assert quote.available is True
    assert quote.stale is True


def test_gold_quote_refuses_missing_price() -> None:
    now = datetime(2026, 8, 28, 5, 0, tzinfo=UTC)
    quote = _quote_from_payload(
        {"price": None, "updatedAt": now.isoformat()},
        now=now,
    )

    assert quote.price is None
    assert quote.available is False
