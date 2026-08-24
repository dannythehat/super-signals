from hashlib import sha256

import pytest

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _decision(*, decision: str, side: str | None = None, low: str | None = None,
              high: str | None = None, sl: str | None = None,
              tps: list[str] | None = None, order_type: str | None = None) -> AiMessageDecision:
    return AiMessageDecision(
        decision=decision,
        action="skip",
        confidence=0.99,
        reason="historical_replay",
        extracted={
            "symbol": "XAUUSD" if side else None,
            "side": side,
            "order_type": order_type,
            "entry_low": low,
            "entry_high": high,
            "stop_loss": sl,
            "take_profits": tps or [],
            "double_lot": False,
            "update_type": None,
            "update_target": None,
            "update_value": None,
            "provider_claimed_pips": None,
            "source_profile": "matthew_xauusd",
        },
        model="fixture",
        response_id=None,
        latency_ms=0,
        source="historical_replay",
        raw_text_sha256=sha256(b"matthew").hexdigest(),
    )


@pytest.mark.parametrize(
    ("raw", "low", "high", "sl", "tps", "layers"),
    [
        ("BUY LIMITS GOLD @ 4050/4044 AREA\nTP 4053\nTP 4057\nTP 4062\nTP OPEN\nSL 4043\nHIGH RISK TRADE",
         "4044", "4050", "4043", ["4053", "4057", "4062"], 7),
        ("BUY LIMITS GOLD @ 4038/4032 AREA\nTP 4041\nTP 4045\nTP 4050\nTP OPEN\nSL 4031\nHIGH RISK TRADE",
         "4032", "4038", "4031", ["4041", "4045", "4050"], 7),
        ("BUY LIMITS GOLD @ 4058/4052 AREA\nTP 4061\nTP 4065\nTP 4070\nTP OPEN\nSL 4051\nHIGH RISK TRADE",
         "4052", "4058", "4051", ["4061", "4065", "4070"], 7),
    ],
)
def test_late_matthew_limits_previously_missed_now_execute(
    raw: str, low: str, high: str, sl: str, tps: list[str], layers: int
) -> None:
    result = apply_v1_message_policy(
        _decision(
            decision="non_actionable", side="BUY", low=low, high=high,
            sl=sl, tps=tps, order_type="pending",
        ),
        raw_text=raw,
    )
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == "v1_complete_layered_signal"
    assert len(result.extracted["entry_plan"]) == layers
    assert result.extracted["double_lot"] is False
    assert result.extracted["tp_open"] is True


@pytest.mark.parametrize("raw", ["PREPARE FOR BUY LIMITS", "PREPARE FOR A BUY"])
def test_matthew_prepare_never_executes(raw: str) -> None:
    result = apply_v1_message_policy(_decision(decision="preparation"), raw_text=raw)
    assert result.decision == "preparation"
    assert result.action == "ignore"


@pytest.mark.parametrize(
    "raw",
    ["TP1 HIT +20 PIPS 🔥\n(4059.2 TO 4061)", "+100 PIPS HIT 🔥", "4050 HIT +230 PIPS 🔥"],
)
def test_matthew_results_never_manufacture_a_broker_close(raw: str) -> None:
    result = apply_v1_message_policy(_decision(decision="trade_update"), raw_text=raw)
    assert result.action == "ignore"


def test_matthew_close_layers_leaves_only_best_entry() -> None:
    raw = "CLOSE 3 LAYERS AND YOU WILL FULLY RECOVER OUR LAST SL.\nLEAVE BEST ENTRY RUNNING"
    result = apply_v1_message_policy(_decision(decision="trade_update"), raw_text=raw)
    assert result.action == "apply_update"
    assert {"type": "close", "target": "worst_3_layers", "value": None} in result.extracted["management_actions"]
    assert {"type": "close", "target": "all_but_best", "value": None} in result.extracted["management_actions"]


def test_matthew_price_specific_closes_and_breakeven_are_preserved() -> None:
    raw = "+40\nRISK FREE 4053\n4053 SL TO BE\n4054 CLOSE +30\n4055 CLOSE +20\n4056 CLOSE +10\n4057 CLOSE -0\n4058 CLOSE -10"
    result = apply_v1_message_policy(_decision(decision="trade_update"), raw_text=raw)
    assert result.action == "apply_update"
    actions = result.extracted["management_actions"]
    for price in ("4054", "4055", "4056", "4057", "4058"):
        assert {"type": "close", "target": f"entry_price_{price}", "value": None} in actions
    assert any(action["type"] in {"edit_stop_loss", "move_to_break_even"} for action in actions)
