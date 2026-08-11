import json

import httpx

from app.ai_message_supervisor import AI_DECISION_SCHEMA, OpenAiMessageSupervisor


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def test_supervisor_uses_strict_structured_output_and_no_storage(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected = {
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
        timeout_seconds=6,
    )
    result = supervisor.decide(
        raw_text="BUY GOLD @ 4371\nTP 4375\nTP 4380\nTP 4385\nSL 4360",
        source_status="testing",
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
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"] == AI_DECISION_SCHEMA


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
