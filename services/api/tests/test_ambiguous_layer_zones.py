import pytest

from app.critical_entry_policy import parse_critical_entries


def test_tdc_plural_pending_zone_fails_closed_instead_of_using_first_number() -> None:
    raw = (
        "BUY LIMITS GOLD @ 4332/4326 AREA\n\n"
        "TP 4335\nTP 4339\nTP 4344\nTP OPEN\nSL 4325\n\nHIGH RISK TRADE"
    )
    with pytest.raises(ValueError, match="pending_layer_grid_unspecified"):
        parse_critical_entries(
            raw,
            side="BUY",
            entry_low="4326",
            entry_high="4332",
        )


def test_explicit_numbered_entry_list_is_supported() -> None:
    raw = (
        "BUY XAUUSD\nENTRY 1: 4394\nENTRY 2: 4390\nENTRY 3: 4386\n"
        "SL: 4378\nTP1: 4400"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low="4386",
        entry_high="4394",
    )
    assert [(item.entry_index, item.order_type, str(item.price)) for item in entries] == [
        (1, "market", "4394"),
        (2, "buy_limit", "4390"),
        (3, "buy_limit", "4386"),
    ]
