from app.critical_entry_policy import parse_critical_entries
from app.day27_management_policy import extract_day27_management_actions
from app.message_classifier import classify_message


def test_tdc_two_explicit_buy_limits_are_two_broker_levels_not_hidden_grid() -> None:
    raw = (
        "BUY LIMITS GOLD @ 4391/4387 AREA\n\n"
        "TP 4393\nTP 4396\nTP 4400\nTP OPEN\nSL 4386"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low="4387",
        entry_high="4391",
    )
    assert [(item.entry_index, item.order_type, str(item.price)) for item in entries] == [
        (1, "buy_limit", "4391"),
        (2, "buy_limit", "4387"),
    ]


def test_high_risk_tdc_grid_keeps_existing_declared_grid_semantics() -> None:
    raw = (
        "HIGH RISK TRADE\n"
        "BUY LIMITS GOLD @ 4391/4387 AREA\n"
        "TP 4393\nTP OPEN\nSL 4386"
    )
    entries = parse_critical_entries(
        raw,
        side="BUY",
        entry_low="4387",
        entry_high="4391",
    )
    assert [str(item.price) for item in entries] == ["4391", "4390", "4389", "4388", "4387"]


def test_tgc_present_tense_numeric_entry_is_never_chatter() -> None:
    result = classify_message("Im selling 4390")
    assert result.classification == "uncertain"
    assert result.decision_status == "review"
    assert "present_tense_numeric_entry" in result.matched_rules


def test_tgc_present_tense_buy_is_never_chatter() -> None:
    result = classify_message("I'm buying 4397")
    assert result.classification == "uncertain"
    assert result.decision_status == "review"


def test_tgc_observed_seling_typo_is_never_chatter() -> None:
    result = classify_message("Im seling 4390")
    assert result.classification == "uncertain"
    assert result.decision_status == "review"
    assert "tgc_present_tense_numeric_entry" in result.matched_rules


def test_tgc_newline_present_tense_entry_is_never_chatter() -> None:
    result = classify_message("Im\nSelling 4404")
    assert result.classification == "uncertain"
    assert result.decision_status == "review"


def test_tgc_conditional_present_tense_entry_reaches_semantic_review() -> None:
    result = classify_message("Im selling if we tap 4390")
    assert result.classification == "uncertain"
    assert result.decision_status == "review"


def test_fx_open_extra_sells_is_explicit_active_trade_management() -> None:
    result = extract_day27_management_actions("OPEN EXTRA GOLD SELLS\n\nFUCK IT.")
    assert {"type": "add_market", "target": "same_trade", "value": "SELL"} in result.actions


def test_fx_open_extra_buys_is_symmetric() -> None:
    result = extract_day27_management_actions("OPEN EXTRA GOLD BUYS")
    assert {"type": "add_market", "target": "same_trade", "value": "BUY"} in result.actions
