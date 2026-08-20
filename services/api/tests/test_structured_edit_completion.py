from hashlib import sha256

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _decision(
    raw: str,
    *,
    side: str,
    entry_low: str,
    entry_high: str,
    stop_loss: str,
    tps: list[str],
) -> AiMessageDecision:
    return AiMessageDecision(
        decision="new_trade",
        action="skip",
        confidence=0.99,
        reason="fixture",
        extracted={
            "symbol": "XAUUSD",
            "side": side,
            "order_type": "market",
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss,
            "take_profits": tps,
            "double_lot": False,
            "update_type": None,
            "update_target": None,
            "update_value": None,
            "provider_claimed_pips": None,
        },
        model="fixture",
        response_id=None,
        latency_ms=1,
        source="openai",
        raw_text_sha256=sha256(raw.encode()).hexdigest(),
    )


def test_tig_structured_edit_can_complete_matching_activation_stub() -> None:
    raw = (
        "🔴SELL  XAUUSD\n\n"
        "ENTRY: 4394\n"
        "Second entry: 4398\n\n"
        "SL: 4410\n"
        "TP1: 4389\n"
        "TP2: 4383\n"
        "TP3: 4377\n"
        "TP4: open\n\n"
        "Manage risk properly."
    )
    result = apply_v1_message_policy(
        _decision(
            raw,
            side="SELL",
            entry_low="4394",
            entry_high="4398",
            stop_loss="4410",
            tps=["4389", "4383", "4377"],
        ),
        raw_text=raw,
        is_edit=True,
        original_has_signal=False,
        previous_text="SELL XAUUSD NOW 4394",
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_layered_signal_from_structured_edit"
    assert result.extracted["tp_open"] is True
    assert result.extracted["entry_plan"] == [
        {"entry_index": 1, "order_type": "market", "price": "4394"},
        {"entry_index": 2, "order_type": "sell_limit", "price": "4398"},
    ]


def test_complete_current_edit_needs_no_prior_activation_stub() -> None:
    raw = (
        "SELL XAUUSD\nENTRY 4394\nSL 4410\n"
        "TP1 4389\nTP2 4383\nTP3 4377\nTP4 OPEN"
    )
    result = apply_v1_message_policy(
        _decision(
            raw,
            side="SELL",
            entry_low="4394",
            entry_high="4394",
            stop_loss="4410",
            tps=["4389", "4383", "4377"],
        ),
        raw_text=raw,
        is_edit=True,
        original_has_signal=False,
        previous_text=None,
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_exact_signal_from_structured_edit"


def test_current_edit_wins_over_intermediate_typo_price() -> None:
    raw = (
        "🔴SELL  XAUUSD\n\n"
        "ENTRY: 4521\nSecond entry: 4525\n\n"
        "SL: 4537\nTP1: 4515\nTP2: 4510\nTP3: 4504\nTP4: open\n\n"
        "Manage risk properly."
    )
    result = apply_v1_message_policy(
        _decision(
            raw,
            side="SELL",
            entry_low="4521",
            entry_high="4525",
            stop_loss="4537",
            tps=["4515", "4510", "4504"],
        ),
        raw_text=raw,
        is_edit=True,
        original_has_signal=False,
        previous_text=(
            "🔴SELL  XAUUSD\n\n"
            "ENTRY: 4421\nSecond entry: 4425\n\n"
            "SL: 4437\nTP1: 4415\nTP2: 4410\nTP3: 4404\nTP4: open"
        ),
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_layered_signal_from_structured_edit"
    assert result.extracted["entry_low"] == "4521"
    assert result.extracted["entry_high"] == "4525"


def test_current_edit_can_correct_side_from_superseded_revision() -> None:
    raw = "SELL XAUUSD\nENTRY 4394\nSL 4410\nTP1 4389"
    result = apply_v1_message_policy(
        _decision(
            raw,
            side="SELL",
            entry_low="4394",
            entry_high="4394",
            stop_loss="4410",
            tps=["4389"],
        ),
        raw_text=raw,
        is_edit=True,
        original_has_signal=False,
        previous_text="BUY GOLD NOW 4394",
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_exact_signal_from_structured_edit"


def test_invalid_current_edit_still_fails_directional_integrity() -> None:
    raw = (
        "🔴SELL XAUUSD\nENTRY: 4509\nSecond entry: 4513\n"
        "SL: 4425\nTP1: 4504\nTP2: 4498\nTP3: 4492\nTP4: open"
    )
    result = apply_v1_message_policy(
        _decision(
            raw,
            side="SELL",
            entry_low="4509",
            entry_high="4513",
            stop_loss="4425",
            tps=["4504", "4498", "4492"],
        ),
        raw_text=raw,
        is_edit=True,
        original_has_signal=False,
        previous_text="SELL GOLD NOW 4509",
    )
    assert result.action == "skip"
    assert result.reason == "strict_directional_validation_failed"
