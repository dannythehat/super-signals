"""Day 17 strict validation acceptance coverage."""

from decimal import Decimal

import pytest

from app.message_review import validate_parsed_trade


def validate(
    direction: str,
    entry: str,
    stop: str,
    targets: list[str],
    size: str = "1",
):
    return validate_parsed_trade(
        symbol="XAUUSD",
        direction=direction,
        entry_price=Decimal(entry),
        stop_loss=Decimal(stop),
        take_profits=[Decimal(value) for value in targets],
        size_multiplier=Decimal(size),
    )


def test_valid_buy_passes_strict_validation() -> None:
    result = validate("BUY", "4060", "4052", ["4072", "4080"])
    assert result.status == "valid"
    assert "target_order" in result.matched_rules


def test_valid_sell_passes_strict_validation() -> None:
    result = validate("SELL", "4047", "4070", ["4043", "4042", "4000"], "2")
    assert result.status == "valid"
    assert "accepted_size_multiplier" in result.matched_rules


@pytest.mark.parametrize(
    ("direction", "entry", "stop", "targets", "rule"),
    [
        ("BUY", "4100", "4110", ["4120"], "buy_stop_not_below_entry"),
        ("BUY", "4100", "4090", ["4095"], "buy_target_not_above_entry"),
        ("BUY", "4100", "4090", ["4120", "4110"], "buy_targets_not_increasing"),
        ("SELL", "4100", "4090", ["4080"], "sell_stop_not_above_entry"),
        ("SELL", "4100", "4110", ["4120"], "sell_target_not_below_entry"),
        ("SELL", "4100", "4110", ["4080", "4090"], "sell_targets_not_decreasing"),
    ],
)
def test_directionally_unsafe_trade_fails(
    direction: str,
    entry: str,
    stop: str,
    targets: list[str],
    rule: str,
) -> None:
    result = validate(direction, entry, stop, targets)
    assert result.status == "failed"
    assert rule in result.matched_rules


def test_equal_prices_fail_instead_of_being_repaired() -> None:
    result = validate("BUY", "4100", "4100", ["4120"])
    assert result.status == "failed"
    assert "buy_stop_not_below_entry" in result.matched_rules


def test_unknown_size_multiplier_fails() -> None:
    result = validate("BUY", "4100", "4090", ["4120"], "3")
    assert result.status == "failed"
    assert "invalid_size_multiplier" in result.matched_rules
