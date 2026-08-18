from dataclasses import replace
from hashlib import sha256

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _decision(*, side: str, entry: str, sl: str | None = None, tps: list[str] | None = None, profile: bool = True) -> AiMessageDecision:
    raw = "fixture"
    extracted = {
        "symbol": "XAUUSD",
        "side": side,
        "order_type": "market",
        "entry_low": entry,
        "entry_high": entry,
        "stop_loss": sl,
        "take_profits": tps or [],
        "double_lot": False,
        "update_type": None,
        "update_target": None,
        "update_value": None,
        "provider_claimed_pips": None,
    }
    if profile:
        extracted["source_profile"] = "tgc_xauusd"
    return AiMessageDecision(
        decision="new_trade",
        action="execute",
        confidence=1.0,
        reason="fixture",
        extracted=extracted,
        model="fixture",
        response_id=None,
        latency_ms=0,
        source="fixture",
        raw_text_sha256=sha256(raw.encode()).hexdigest(),
    )


def test_tgc_shorthand_uses_source_identity_for_xauusd_but_still_requires_sl() -> None:
    result = apply_v1_message_policy(
        _decision(side="BUY", entry="4395"),
        raw_text="Im buying 4395",
    )
    assert result.action == "skip"
    assert result.reason == "missing_sl"
    assert result.extracted["symbol"] == "XAUUSD"
    assert result.extracted["source_profile"] == "tgc_xauusd"


def test_tgc_complete_shorthand_can_execute_without_repeating_gold_token() -> None:
    result = apply_v1_message_policy(
        _decision(side="BUY", entry="4395", sl="4385", tps=["4398", "4400"]),
        raw_text="Im buying 4395\nSL 4385\nTP 4398\nTP 4400",
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_exact_signal"
    assert result.extracted["symbol"] == "XAUUSD"


def test_tgc_observed_seling_typo_is_mechanically_normalized() -> None:
    result = apply_v1_message_policy(
        _decision(side="SELL", entry="4390", sl="4400", tps=["4388"]),
        raw_text="Im seling 4390\nSL 4400\nTP 4388",
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_exact_signal"


def test_tgc_observed_sellimg_typo_is_mechanically_normalized() -> None:
    result = apply_v1_message_policy(
        _decision(side="SELL", entry="4392", sl="4402", tps=["4390"]),
        raw_text="Im sellimg 4392\nSL 4402\nTP 4390",
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_exact_signal"


def test_other_source_cannot_borrow_tgc_instrument_profile() -> None:
    result = apply_v1_message_policy(
        _decision(side="BUY", entry="4395", sl="4385", tps=["4398"], profile=False),
        raw_text="Im buying 4395\nSL 4385\nTP 4398",
    )
    assert result.action == "skip"
    assert result.reason == "missing_instrument"
