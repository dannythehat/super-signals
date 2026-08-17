from hashlib import sha256

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _decision(raw: str, *, side: str, entry_low: str, entry_high: str, stop_loss: str, tps: list[str]) -> AiMessageDecision:
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


def test_tig_structured_edit_can_create_first_layered_signal() -> None:
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
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_layered_signal_from_structured_edit"
    assert result.extracted["tp_open"] is True
    assert result.extracted["entry_plan"] == [
        {"entry_index": 1, "order_type": "market", "price": "4394"},
        {"entry_index": 2, "order_type": "sell_limit", "price": "4398"},
    ]


def test_unstructured_edit_still_cannot_create_first_trade() -> None:
    raw = "SELL GOLD NOW 4394"
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
    )
    assert result.action == "skip"
    assert result.reason == "edit_cannot_create_first_trade"
