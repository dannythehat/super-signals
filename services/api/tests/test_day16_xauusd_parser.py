"""Day 16 acceptance coverage for the first XAUUSD parser."""

from decimal import Decimal

import pytest

from app.xauusd_parser import parse_xauusd_trade


def test_canonical_provider_example_parses_exactly() -> None:
    result = parse_xauusd_trade(
        "XAUUSD SELL 4047\n"
        "TP1 4043\n"
        "TP2 4042\n"
        "TP3 4000\n"
        "SL 4070\n"
        "USE DOUBLE LOT SIZE"
    )
    assert result.status == "parsed"
    assert result.trade is not None
    assert result.trade.symbol == "XAUUSD"
    assert result.trade.direction == "SELL"
    assert result.trade.entry == Decimal("4047")
    assert result.trade.stop_loss == Decimal("4070")
    assert result.trade.take_profits == (
        Decimal("4043"),
        Decimal("4042"),
        Decimal("4000"),
    )
    assert result.trade.size_multiplier == Decimal("2")


@pytest.mark.parametrize(
    ("raw_text", "direction", "targets"),
    [
        (
            "XAUUSD BUY 3360\nSL 3350\nTP1 3370",
            "BUY",
            (Decimal("3370"),),
        ),
        (
            "buy xauusd @ 3360.5\n sl 3350.25 \n tp1 3370 \n tp2 3380",
            "BUY",
            (Decimal("3370"), Decimal("3380")),
        ),
        (
            "XAUUSD SELL 4047\nSTOP LOSS 4070\nTP1 4043\nTP2 4000",
            "SELL",
            (Decimal("4043"), Decimal("4000")),
        ),
    ],
)
def test_known_format_variations_parse(raw_text: str, direction: str, targets: tuple[Decimal, ...]) -> None:
    result = parse_xauusd_trade(raw_text)
    assert result.status == "parsed"
    assert result.trade is not None
    assert result.trade.direction == direction
    assert result.trade.take_profits == targets


@pytest.mark.parametrize(
    ("raw_text", "rule"),
    [
        ("XAUUSD 4047\nSL 4070\nTP1 4043", "invalid_header"),
        ("XAUUSD SELL\nSL 4070\nTP1 4043", "invalid_header"),
        ("XAUUSD SELL 4047\nTP1 4043", "missing_stop_loss"),
        ("XAUUSD SELL 4047\nSL 4070", "missing_take_profit"),
        ("XAUUSD SELL 4047\nSL 4070\nTP1 4043\nTP3 4000", "non_contiguous_take_profits"),
        ("XAUUSD SELL 4047\nSL 4070\nSL 4080\nTP1 4043", "duplicate_stop_loss"),
        ("XAUUSD SELL 4047\nSL 4070\nTP1 4043\nTP1 4042", "duplicate_take_profit"),
        ("XAUUSD SELL 4047\nSL 4070\nTP1 4043\nDOUBLE SIZE MAYBE", "unrecognised_field"),
        ("GOLD SELL 4047\nSL 4070\nTP1 4043", "invalid_header"),
    ],
)
def test_malformed_or_out_of_scope_examples_fail_safely(raw_text: str, rule: str) -> None:
    result = parse_xauusd_trade(raw_text)
    assert result.status == "failed"
    assert result.trade is None
    assert rule in result.matched_rules
