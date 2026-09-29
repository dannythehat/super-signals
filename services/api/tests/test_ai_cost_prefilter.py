from app.ai_cost_prefilter import deterministic_ai_cost_prefilter


def _prefilter(
    text: str,
    *,
    source_name: str | None = "Example Gold",
    reply_context: str | None = None,
    is_edit: bool = False,
    active: bool = False,
):
    return deterministic_ai_cost_prefilter(
        raw_text=text,
        source_name=source_name,
        reply_context=reply_context,
        is_edit=is_edit,
        has_active_trade_context=active,
    )


def test_obvious_social_chatter_is_free_filtered() -> None:
    decision = _prefilter("Thanks boss, have a blessed day!")
    assert decision is not None
    assert decision.decision == "chatter"
    assert decision.action == "ignore"
    assert decision.source == "deterministic_no_ai"
    assert decision.reason == "deterministic_obvious_chatter_prefilter"


def test_preparation_without_trade_geometry_is_free_filtered() -> None:
    decision = _prefilter("Ready")
    assert decision is not None
    assert decision.decision == "preparation"
    assert decision.action == "ignore"


def test_marketing_with_rhetorical_buy_sell_is_free_filtered() -> None:
    decision = _prefilter(
        "New trade on GOLD shared in the VIP. Which direction? Buy or Sell? "
        "CLICK HERE TO CLAIM YOUR SPOT & UNLOCK ALL THE TRADES"
    )
    assert decision is not None
    assert decision.decision == "chatter"
    assert decision.reason == "deterministic_marketing_prefilter"


def test_result_report_that_was_historically_ignored_is_free_filtered() -> None:
    decision = _prefilter("TP1 ✅ / 15 pips 💷")
    assert decision is not None
    assert decision.decision == "trade_update"
    assert decision.action == "ignore"
    assert decision.reason == "deterministic_result_report_only_prefilter"


def test_optional_risk_free_advice_is_not_treated_as_instruction() -> None:
    decision = _prefilter("Trade in +40 pips. Make the trade risk-free if you want ✅")
    assert decision is not None
    assert decision.decision == "trade_update"
    assert decision.action == "ignore"


def test_known_gold_vip_price_pulse_is_free_filtered() -> None:
    decision = _prefilter("4113.50🚀🚀", source_name="GOLD VIP (XAUUSD)")
    assert decision is not None
    assert decision.decision == "chatter"
    assert decision.reason == "deterministic_price_pulse_prefilter"


def test_naked_price_from_other_provider_still_requires_ai() -> None:
    assert _prefilter("4113.50🚀🚀", source_name="Another Provider") is None


def test_ambiguous_gold_price_still_requires_ai() -> None:
    assert _prefilter("Gold 4113.50", source_name="Another Provider") is None


def test_terse_provider_entry_still_requires_ai() -> None:
    assert _prefilter("I'm buying 4113.50") is None


def test_management_language_still_requires_ai() -> None:
    assert _prefilter("close gold now") is None
    assert _prefilter("move to breakeven") is None
    assert _prefilter("secure here") is None


def test_direct_reply_always_keeps_semantic_ai() -> None:
    assert _prefilter("yes", reply_context="SELL GOLD 4118") is None


def test_generic_edit_keeps_semantic_ai() -> None:
    assert _prefilter("Thanks boss", is_edit=True) is None


def test_generic_message_with_active_trade_keeps_semantic_ai() -> None:
    assert _prefilter("both done", active=True) is None


def test_unmistakable_result_noise_can_filter_even_with_active_trade() -> None:
    decision = _prefilter("Profits from layering 💰\n\n14/15/16/17", active=True)
    assert decision is not None
    assert decision.decision == "trade_update"
    assert decision.action == "ignore"
