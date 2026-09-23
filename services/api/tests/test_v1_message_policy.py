from hashlib import sha256

import pytest

from app.ai_message_supervisor import AiMessageDecision
from app.v1_message_policy import apply_v1_message_policy


def _decision(
    *,
    decision: str = "new_trade",
    action: str = "skip",
    symbol: str | None = "XAUUSD",
    side: str | None = "BUY",
    order_type: str | None = "market",
    entry_low: str | None = None,
    entry_high: str | None = None,
    stop_loss: str | None = None,
    take_profits: list[str] | None = None,
    update_type: str | None = None,
) -> AiMessageDecision:
    raw = "fixture"
    return AiMessageDecision(
        decision=decision,
        action=action,
        confidence=0.99,
        reason="ai_fixture",
        extracted={
            "symbol": symbol,
            "side": side,
            "order_type": order_type,
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss,
            "take_profits": take_profits or [],
            "double_lot": False,
            "update_type": update_type,
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


def test_complete_exact_signal_executes() -> None:
    raw = "BUY GOLD @ 4394\nSL 4385\nTP 4398\nTP 4402"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4394",
            entry_high="4394",
            stop_loss="4385",
            take_profits=["4398", "4402"],
        ),
        raw_text=raw,
    )
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == "v1_complete_exact_signal"


def test_complete_dash_zone_executes() -> None:
    raw = "Buy Gold Now\n4394 - 4389\nTP 4396.5\nTP 4399\nSL 4386"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4389",
            entry_high="4394",
            stop_loss="4386",
            take_profits=["4396.5", "4399"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_zone_signal"
    assert result.extracted["entry_low"] == "4389"
    assert result.extracted["entry_high"] == "4394"


def test_complete_slash_zone_executes() -> None:
    raw = "BUY GOLD @ 4396/4391\nSL 4390\nTP 4399\nTP 4402"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4391",
            entry_high="4396",
            stop_loss="4390",
            take_profits=["4399", "4402"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_zone_signal"


def test_missing_sl_skips() -> None:
    raw = "BUY GOLD @ 4394\nTP 4398"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4394",
            entry_high="4394",
            stop_loss=None,
            take_profits=["4398"],
        ),
        raw_text=raw,
    )
    assert result.action == "skip"
    assert result.reason == "missing_sl"


def test_missing_tp_skips() -> None:
    raw = "BUY GOLD @ 4394\nSL 4385"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4394",
            entry_high="4394",
            stop_loss="4385",
        ),
        raw_text=raw,
    )
    assert result.action == "skip"
    assert result.reason == "missing_tp"


def test_missing_instrument_skips_even_if_ai_inferred_gold_from_context() -> None:
    raw = "BUY @ 4394\nSL 4385\nTP 4398"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4394",
            entry_high="4394",
            stop_loss="4385",
            take_profits=["4398"],
        ),
        raw_text=raw,
    )
    assert result.action == "skip"
    assert result.reason == "missing_instrument"


def test_context_donated_sl_or_tp_cannot_pass_literal_gate() -> None:
    raw = "BUY GOLD @ 4394"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4394",
            entry_high="4394",
            stop_loss="4385",
            take_profits=["4398"],
        ),
        raw_text=raw,
    )
    assert result.action == "skip"
    assert result.reason == "literal_value_verification_failed"


def test_pending_limit_executes_with_literal_broker_plan() -> None:
    raw = "BUY LIMIT GOLD @ 4394\nSL 4385\nTP 4398"
    result = apply_v1_message_policy(
        _decision(
            order_type="pending",
            entry_low="4394",
            entry_high="4394",
            stop_loss="4385",
            take_profits=["4398"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_pending_signal"
    assert result.extracted["order_type"] == "pending"
    assert result.extracted["entry_plan"] == [
        {"entry_index": 1, "order_type": "buy_limit", "price": "4394"}
    ]


def test_tig_second_entry_is_preserved_as_second_broker_layer() -> None:
    raw = (
        "🟢BUY XAUUSD\n"
        "ENTRY: 4375\n"
        "Second entry: 4370\n"
        "SL: 4361\n"
        "TP1: 4381\n"
        "TP2: 4386\n"
        "TP3: 4391"
    )
    result = apply_v1_message_policy(
        _decision(
            entry_low="4370",
            entry_high="4375",
            stop_loss="4361",
            take_profits=["4381", "4386", "4391"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.reason == "v1_complete_layered_signal"
    assert result.extracted["entry_low"] == "4370"
    assert result.extracted["entry_high"] == "4375"
    assert result.extracted["entry_plan"] == [
        {"entry_index": 1, "order_type": "market", "price": "4375"},
        {"entry_index": 2, "order_type": "buy_limit", "price": "4370"},
    ]


def test_tp_open_with_numeric_targets_adds_runner_marker() -> None:
    raw = "BUY GOLD @ 4394\nSL 4385\nTP 4398\nTP 4402\nTP OPEN"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4394",
            entry_high="4394",
            stop_loss="4385",
            take_profits=["4398", "4402"],
        ),
        raw_text=raw,
    )
    assert result.action == "execute"
    assert result.extracted["tp_open"] is True
    assert result.extracted["take_profits"] == ["4398", "4402"]


def test_tp_open_without_numeric_tp_skips() -> None:
    raw = "BUY GOLD @ 4394\nSL 4385\nTP OPEN"
    result = apply_v1_message_policy(
        _decision(
            entry_low="4394",
            entry_high="4394",
            stop_loss="4385",
            take_profits=[],
        ),
        raw_text=raw,
    )
    assert result.action == "skip"
    assert result.reason == "missing_tp"


def test_provider_result_out_at_be_is_not_close_instruction() -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update", update_type="close"),
        raw_text="Out at BE ✅",
    )
    assert result.action == "ignore"
    assert result.reason == "provider_result_only"


@pytest.mark.parametrize("raw", ["BE now", "Breakeven set!", "Make the trade risk free now"])
def test_supported_break_even_phrases_are_management(raw: str) -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update"),
        raw_text=raw,
    )
    assert result.action == "apply_update"
    assert result.extracted["update_type"] == "move_to_break_even"


def test_bare_be_is_too_ambiguous_to_mutate_broker_state() -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update"),
        raw_text="BE",
    )
    assert result.action == "ignore"
    assert result.reason == "unsupported_management"


@pytest.mark.parametrize("raw", ["Close all!!", "Closed all!!", "Out at entry on the rest"])
def test_supported_close_phrases_are_management(raw: str) -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update"),
        raw_text=raw,
    )
    assert result.action == "apply_update"
    assert result.extracted["update_type"] == "close"


def test_cancel_is_supported_management_event() -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update"),
        raw_text="Cancel this",
    )
    assert result.action == "apply_update"
    assert result.extracted["update_type"] == "cancel_pending"


@pytest.mark.parametrize("raw", ["+40 pips", "TP2 incoming"])
def test_non_terminal_results_and_hype_do_not_create_management_action(raw: str) -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update", update_type="other"),
        raw_text=raw,
    )
    assert result.action == "ignore"


@pytest.mark.parametrize(
    ("raw", "target"),
    [
        ("TP1 HIT", "TP1"),
        ("Hit TP2", "TP2"),
        ("TP All HIT", "all"),
        ("SL HIT", "all"),
        ("Stopped out", "all"),
    ],
)
def test_provider_terminal_results_are_management(raw: str, target: str) -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="ignore", update_type="other"),
        raw_text=raw,
    )
    assert result.action == "apply_update"
    assert {"type": "close", "target": target, "value": None} in result.extracted[
        "management_actions"
    ]


def test_bare_sl_price_is_management() -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="ignore", update_type="other"),
        raw_text="SL : 4351",
    )
    assert result.action == "apply_update"
    assert result.extracted["management_actions"] == [
        {"type": "edit_stop_loss", "target": "all", "value": "4351"}
    ]


def test_partial_taking_preserves_partial_intent() -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update", update_type="close_half"),
        raw_text="Take some profit now",
    )
    assert result.action == "apply_update"
    assert {"type": "close", "target": "partial_tp1", "value": None} in result.extracted[
        "management_actions"
    ]
    assert result.reason == "day27_explicit_management"


def test_second_entry_partial_and_stop_update_are_scoped_to_layer_two() -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update"),
        raw_text="Second entry is running +50 pips, Book partial. Move SL to 4373",
    )
    assert result.action == "apply_update"
    assert {"type": "close", "target": "entry_2_partial_tp1", "value": None} in result.extracted[
        "management_actions"
    ]
    assert {
        "type": "edit_stop_loss",
        "target": "entry_2",
        "value": "4373",
    } in result.extracted["management_actions"]


def test_close_layers_leave_best_is_preserved_as_layer_target() -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update"),
        raw_text="CLOSE 3 LAYERS NOW TO FULLY RECOVER THE SL AND LEAVE BEST RUNNING",
    )
    assert result.action == "apply_update"
    assert result.extracted["management_actions"][0] == {
        "type": "close",
        "target": "worst_3_layers",
        "value": None,
    }


@pytest.mark.parametrize("decision", ["chatter", "preparation"])
def test_chatter_and_preparation_never_execute(decision: str) -> None:
    result = apply_v1_message_policy(
        _decision(decision=decision, action="ignore"),
        raw_text="Gold is around 4395 and looks strong",
    )
    assert result.action == "ignore"


@pytest.mark.parametrize(
    ("raw", "side", "entry_low", "entry_high", "stop_loss", "take_profits"),
    [
        (
            "🟢BUY XAUUSD\nENTRY: 4618\nSecond entry: 4614\nSL: 4601\n"
            "TP1: 4623\nTP2: 4629\nTP3: 4635\nTP4: open",
            "BUY", "4614", "4618", "4601", ["4623", "4629", "4635"],
        ),
        (
            "🔴SELL XAUUSD\nENTRY: 4653\nSecond entry: 4657\nSL: 4670\n"
            "TP1: 4647\nTP2: 4642\nTP3: 4636\nTP4: open",
            "SELL", "4653", "4657", "4670", ["4647", "4642", "4636"],
        ),
    ],
)
def test_complete_tig_edit_cannot_be_downgraded_to_trade_update(
    raw: str,
    side: str,
    entry_low: str,
    entry_high: str,
    stop_loss: str,
    take_profits: list[str],
) -> None:
    result = apply_v1_message_policy(
        _decision(
            decision="trade_update",
            action="ignore",
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
        ),
        raw_text=raw,
        is_edit=True,
        original_has_signal=False,
    )
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == "v1_complete_layered_signal_from_structured_edit"


def test_complete_matthew_pending_layers_cannot_be_downgraded_to_non_actionable() -> None:
    raw = (
        "BUY LIMITS GOLD @ 4052/4047 AREA\n"
        "TP 4054\nTP 4057\nTP 4061\nTP OPEN\nSL 4046\nHIGH RISK TRADE"
    )
    result = apply_v1_message_policy(
        _decision(
            decision="non_actionable",
            action="skip",
            order_type="pending",
            entry_low="4047",
            entry_high="4052",
            stop_loss="4046",
            take_profits=["4054", "4057", "4061"],
        ),
        raw_text=raw,
    )
    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.reason == "v1_complete_layered_signal"
    assert result.extracted["double_lot"] is False
    assert len(result.extracted["entry_plan"]) == 6


def test_matthew_preparation_remains_non_executable() -> None:
    result = apply_v1_message_policy(
        _decision(
            decision="preparation",
            action="ignore",
            side=None,
            entry_low=None,
            entry_high=None,
            stop_loss=None,
            take_profits=[],
        ),
        raw_text="PREPARE FOR BUY LIMITS",
    )
    assert result.decision == "preparation"
    assert result.action == "ignore"


def test_incomplete_update_is_not_promoted() -> None:
    result = apply_v1_message_policy(
        _decision(
            decision="trade_update",
            action="ignore",
            entry_low="4618",
            entry_high="4618",
            stop_loss=None,
            take_profits=[],
        ),
        raw_text="BUY GOLD NOW 4618",
    )
    assert result.decision == "trade_update"
    assert result.action == "ignore"


def test_live_tig_tp2_hit_book_partial_stays_on_tp2_through_v1_policy() -> None:
    result = apply_v1_message_policy(
        _decision(decision="trade_update", action="apply_update"),
        raw_text="Tp2 hits with +110pips✅\n\nBook partial 🤑💰🤑",
    )
    assert result.action == "apply_update"
    assert {"type": "close", "target": "TP2", "value": None} in result.extracted[
        "management_actions"
    ]
    assert {"type": "close", "target": "partial_tp1", "value": None} not in result.extracted[
        "management_actions"
    ]
    assert {"type": "close", "target": "TP1", "value": None} not in result.extracted[
        "management_actions"
    ]
