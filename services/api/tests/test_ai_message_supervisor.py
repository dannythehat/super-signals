import json

import httpx

from app.ai_message_supervisor import (
    AI_DECISION_SCHEMA,
    OpenAiMessageSupervisor,
    _guard_execute_decision,
)


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def _complete_trade_decision() -> dict:
    return {
        "decision": "new_trade",
        "action": "execute",
        "confidence": 0.99,
        "reason": "Provider explicitly posted a complete XAUUSD buy instruction.",
        "symbol": "XAUUSD",
        "side": "BUY",
        "order_type": "market",
        "entry_low": "4371",
        "entry_high": "4371",
        "stop_loss": "4360",
        "take_profits": ["4375", "4380", "4385"],
        "double_lot": False,
        "update_type": None,
        "update_target": None,
        "update_value": None,
        "provider_claimed_pips": None,
    }


def test_supervisor_uses_strict_structured_output_and_no_storage(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected = _complete_trade_decision()

    def fake_post(url: str, *, headers: dict, json: dict, timeout: int):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse(
            {
                "id": "resp_test",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": __import__("json").dumps(expected)}
                        ],
                    }
                ],
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    supervisor = OpenAiMessageSupervisor(
        api_key="test-only",
        model="gpt-5-mini-2025-08-07",
        timeout_seconds=12,
    )
    result = supervisor.decide(
        raw_text="BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4360",
        source_status="testing",
        source_name="Example Gold Provider",
        recent_source_messages=[
            {"telegram_message_id": 10, "text": "PREPARE FOR A BUY"},
        ],
    )

    assert result.decision == "new_trade"
    assert result.action == "execute"
    assert result.extracted["symbol"] == "XAUUSD"
    assert result.extracted["take_profits"] == ["4375", "4380", "4385"]
    assert result.response_id == "resp_test"

    request = captured["json"]
    assert isinstance(request, dict)
    assert request["store"] is False
    assert request["model"] == "gpt-5-mini-2025-08-07"
    assert request["reasoning"] == {"effort": "minimal"}
    assert request["max_output_tokens"] == 1200
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"] == AI_DECISION_SCHEMA
    assert "SOURCE-AWARE INTERPRETATION" in request["instructions"]
    assert "DECISION AND ACTION ARE DIFFERENT QUESTIONS" in request["instructions"]
    assert "CURRENT EXECUTION CAPABILITIES" in request["instructions"]
    assert "explicit second/layered entries" in request["instructions"]
    assert "literal BUY/SELL LIMIT or STOP orders" in request["instructions"]
    assert "complete structured edit" in request["instructions"]
    prompt = json.loads(request["input"])
    assert prompt["source_name"] == "Example Gold Provider"
    assert prompt["provider_language_profile"] is None
    assert prompt["recent_source_messages"][0]["telegram_message_id"] == 10
    assert captured["timeout"] == 12


def test_edit_context_is_sent_as_same_message_revision(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected = _complete_trade_decision()
    expected.update(
        {
            "decision": "trade_update",
            "action": "apply_update",
            "reason": "Provider edited the existing signal stop loss.",
            "stop_loss": "4358",
            "update_type": "edit_stop_loss",
            "update_target": "SL",
            "update_value": "4358",
        }
    )

    def fake_post(url: str, *, headers: dict, json: dict, timeout: int):
        captured["request"] = json
        return FakeResponse(
            {
                "id": "resp_edit",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": __import__("json").dumps(expected)}
                        ],
                    }
                ],
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    supervisor = OpenAiMessageSupervisor(
        api_key="test-only",
        model="gpt-5-mini-2025-08-07",
        timeout_seconds=12,
    )
    result = supervisor.decide(
        raw_text="BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4358",
        source_status="testing",
        previous_text="BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4360",
        is_edit=True,
    )

    assert result.decision == "trade_update"
    assert result.action == "apply_update"
    request = captured["request"]
    assert isinstance(request, dict)
    prompt = json.loads(request["input"])
    assert prompt["is_edit"] is True
    assert prompt["previous_text"].endswith("SL 4360")
    assert prompt["telegram_message"].endswith("SL 4358")


def test_schema_forces_immediate_machine_decision_fields() -> None:
    required = set(AI_DECISION_SCHEMA["required"])
    assert {"decision", "action", "confidence", "reason"}.issubset(required)
    assert "review" not in AI_DECISION_SCHEMA["properties"]["action"]["enum"]
    assert AI_DECISION_SCHEMA["properties"]["action"]["enum"] == [
        "execute",
        "apply_update",
        "ignore",
        "skip",
    ]


def test_schema_preserves_real_provider_format_details() -> None:
    props = AI_DECISION_SCHEMA["properties"]
    for field in (
        "entry_low",
        "entry_high",
        "stop_loss",
        "take_profits",
        "double_lot",
        "order_type",
        "update_type",
    ):
        assert field in props


def test_response_output_parser_reads_responses_api_message_content() -> None:
    body = {
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps({"decision": "chatter"})}
                ],
            },
        ]
    }
    assert json.loads(OpenAiMessageSupervisor._output_text(body))["decision"] == "chatter"


def test_execution_guard_keeps_literal_exact_market_trade_executable() -> None:
    decision = _complete_trade_decision()
    guarded = _guard_execute_decision(
        decision,
        "BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4360",
    )
    assert guarded["action"] == "execute"
    assert guarded["double_lot"] is False


def test_execution_guard_rejects_model_hallucinated_numeric_value() -> None:
    decision = _complete_trade_decision()
    decision["take_profits"] = ["4375", "4380", "4390"]
    guarded = _guard_execute_decision(
        decision,
        "BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4360",
    )
    assert guarded["action"] == "skip"
    assert guarded["reason"] == "literal_value_verification_failed"


def test_execution_guard_rejects_invalid_buy_stop_direction() -> None:
    decision = _complete_trade_decision()
    decision["stop_loss"] = "4372"
    guarded = _guard_execute_decision(
        decision,
        "BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4372",
    )
    assert guarded["action"] == "skip"
    assert guarded["reason"] == "strict_directional_validation_failed"


def test_execution_guard_rejects_entry_range_even_if_model_says_execute() -> None:
    decision = _complete_trade_decision()
    decision.update({"entry_low": "4391", "entry_high": "4396", "stop_loss": "4390"})
    decision["take_profits"] = ["4399", "4403", "4408"]
    guarded = _guard_execute_decision(
        decision,
        "BUY GOLD @ 4396/4391\nTP 4399\nTP 4403\nTP 4408\nSL 4390",
    )
    assert guarded["action"] == "skip"
    assert guarded["reason"] == "unsupported_entry_range"


def test_execution_guard_rejects_second_entry_even_if_model_says_execute() -> None:
    decision = _complete_trade_decision()
    decision.update({"entry_low": "4380", "entry_high": "4385", "stop_loss": "4371"})
    decision["take_profits"] = ["4391", "4396", "4400"]
    guarded = _guard_execute_decision(
        decision,
        "BUY XAUUSD\nENTRY: 4385\nSecond entry: 4380\nSL: 4371\nTP1: 4391\nTP2: 4396\nTP3: 4400",
    )
    assert guarded["action"] == "skip"
    assert guarded["reason"] == "unsupported_multiple_entries"


def test_execution_guard_rejects_pending_order_even_if_model_says_execute() -> None:
    decision = _complete_trade_decision()
    decision.update({"order_type": "pending", "entry_low": "4387", "entry_high": "4387", "stop_loss": "4381"})
    decision["take_profits"] = ["4391", "4396"]
    guarded = _guard_execute_decision(
        decision,
        "BUY LIMIT GOLD @ 4387\nTP 4391\nTP 4396\nSL 4381",
    )
    assert guarded["action"] == "skip"
    assert guarded["reason"] == "unsupported_pending_order"


def test_execution_guard_rejects_open_target_even_if_model_says_execute() -> None:
    decision = _complete_trade_decision()
    decision.update({"entry_low": "4385", "entry_high": "4385", "stop_loss": "4371"})
    decision["take_profits"] = ["4391", "4396"]
    guarded = _guard_execute_decision(
        decision,
        "BUY GOLD @ 4385\nTP 4391\nTP 4396\nTP OPEN\nSL 4371",
    )
    assert guarded["action"] == "skip"
    assert guarded["reason"] == "unsupported_open_target"


def test_execution_guard_does_not_infer_double_size_from_high_risk() -> None:
    decision = _complete_trade_decision()
    decision["double_lot"] = True
    guarded = _guard_execute_decision(
        decision,
        "BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4360\nHIGH RISK TRADE",
    )
    assert guarded["action"] == "execute"
    assert guarded["double_lot"] is False


def test_execution_guard_can_use_direct_reply_but_not_ambient_history() -> None:
    decision = _complete_trade_decision()
    current = "BUY GOLD NOW"

    without_link = _guard_execute_decision(decision, current)
    assert without_link["action"] == "skip"
    assert without_link["reason"] == "literal_value_verification_failed"

    with_link = _guard_execute_decision(
        decision,
        current,
        linked_context="BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4360",
    )
    assert with_link["action"] == "execute"
