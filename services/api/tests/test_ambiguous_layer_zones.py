from app.critical_entry_policy import parse_critical_entries


def test_unproven_plural_pending_zone_falls_back_to_plain_zone_instead_of_skipping() -> None:
    """An unrecognized pending-layer dialect must not skip an otherwise complete signal.

    Before layering support existed, V1 had no pending-order concept at all and this
    exact wording would simply have been read as an ordinary zone entry using the
    entry_low/entry_high already extracted upstream. Regression: on 17 Aug 2026 this
    was changed to raise/skip, which silently stopped otherwise-executable signals.
    """
    raw = (
        "BUY LIMITS GOLD @ 4332/4326 AREA\n\n"
        "TP 4335\nTP 4339\nTP 4344\nSL 4325"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low="4326",
        entry_high="4332",
    )
    assert entries == ()


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
