import json

import httpx

from app.ai_message_supervisor_day34 import Day34OpenAiMessageSupervisor


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def _decision(**overrides: object) -> dict:
    value = {
        "decision": "trade_update",
        "action": "apply_update",
        "confidence": 0.99,
        "reason": "Active broker-backed XAUUSD trade makes this an explicit management update.",
        "symbol": "XAUUSD",
        "side": None,
        "order_type": None,
        "entry_low": None,
        "entry_high": None,
        "stop_loss": None,
        "take_profits": [],
        "double_lot": False,
        "update_type": "move_to_break_even",
        "update_target": "all",
        "update_value": None,
        "provider_claimed_pips": None,
    }
    value.update(overrides)
    return value


def _response(decision: dict) -> FakeResponse:
    return FakeResponse(
        {
            "id": "resp_day34",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(decision)}
                    ],
                }
            ],
        }
    )


def test_active_trade_context_is_explicit_same_source_ai_input(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, headers: dict, json: dict, timeout: int):
        captured["request"] = json
        return _response(_decision())

    monkeypatch.setattr(httpx, "post", fake_post)
    supervisor = Day34OpenAiMessageSupervisor(
        api_key="test-only",
        model="gpt-5-mini-2025-08-07",
        timeout_seconds=12,
    )
    active = [
        {
            "signal_id": "11111111-1111-1111-1111-111111111111",
            "broker_state": "open",
            "symbol": "XAUUSD",
            "side": "BUY",
            "provider_message_id": 1196,
            "provider_entry_low": "4388",
            "provider_entry_high": "4392",
            "provider_stop_loss": "4382",
            "provider_take_profits": ["4396", "4400", "4404"],
            "open_tp_indices": [2, 3],
            "pending_tp_indices": [],
            "broker_reconciled_current_stop_losses": ["4388"],
            "recent_lifecycle": [
                {"event_type": "take_profit_hit", "rendered_text": "TP1 reached."}
            ],
        }
    ]

    result = supervisor.decide_with_active_context(
        raw_text="BE now",
        source_status="live",
        source_name="Example provider",
        active_trade_context=active,
        recent_source_messages=[{"telegram_message_id": 1198, "text": "TP1 done"}],
    )

    assert result.decision == "trade_update"
    assert result.action == "apply_update"
    assert result.extracted["update_type"] == "move_to_break_even"
    request = captured["request"]
    assert isinstance(request, dict)
    assert "ACTIVE TRADE WATCH — DAY 34" in request["instructions"]
    prompt = json.loads(request["input"])
    assert prompt["source_name"] == "Example provider"
    assert prompt["telegram_message"] == "BE now"
    assert prompt["active_trade_context"] == active
    assert "user_id" not in json.dumps(prompt["active_trade_context"])
    assert "account_id" not in json.dumps(prompt["active_trade_context"])
    assert "pnl" not in json.dumps(prompt["active_trade_context"]).lower()


def test_active_context_cannot_supply_missing_new_trade_execution_values(monkeypatch) -> None:
    captured: dict[str, object] = {}
    model_decision = _decision(
        decision="new_trade",
        action="execute",
        reason="model attempted to use context values",
        side="BUY",
        order_type="market",
        entry_low="4371",
        entry_high="4371",
        stop_loss="4360",
        take_profits=["4375", "4380"],
        update_type=None,
        update_target=None,
    )

    def fake_post(url: str, *, headers: dict, json: dict, timeout: int):
        captured["request"] = json
        return _response(model_decision)

    monkeypatch.setattr(httpx, "post", fake_post)
    supervisor = Day34OpenAiMessageSupervisor(
        api_key="test-only",
        model="gpt-5-mini-2025-08-07",
        timeout_seconds=12,
    )
    result = supervisor.decide_with_active_context(
        raw_text="BUY GOLD NOW",
        source_status="live",
        active_trade_context=[
            {
                "signal_id": "11111111-1111-1111-1111-111111111111",
                "symbol": "XAUUSD",
                "provider_entry_low": "4371",
                "provider_entry_high": "4371",
                "provider_stop_loss": "4360",
                "provider_take_profits": ["4375", "4380"],
                "open_tp_indices": [1, 2],
            }
        ],
    )

    assert result.decision == "new_trade"
    assert result.action == "skip"
    # The model tried to copy execution numbers from active context into a terse
    # new-trade instruction. The mechanical literal-evidence guard catches this
    # specifically: none of those values appeared in the current message/direct reply.
    assert result.reason == "literal_value_verification_failed"


def test_multiple_active_trades_are_given_to_ai_without_forcing_a_target(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, *, headers: dict, json: dict, timeout: int):
        captured["request"] = json
        return _response(
            _decision(
                reason="Management intent is clear but target must be resolved fail-closed downstream.",
                update_type="move_to_break_even",
                update_target="all",
            )
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    supervisor = Day34OpenAiMessageSupervisor(
        api_key="test-only",
        model="gpt-5-mini-2025-08-07",
    )
    active = [
        {"signal_id": "a", "broker_state": "open", "symbol": "XAUUSD", "open_tp_indices": [2]},
        {"signal_id": "b", "broker_state": "open", "symbol": "XAUUSD", "open_tp_indices": [1, 2, 3]},
    ]
    result = supervisor.decide_with_active_context(
        raw_text="move to BE",
        source_status="live",
        active_trade_context=active,
    )

    assert result.decision == "trade_update"
    assert result.action == "apply_update"
    prompt = json.loads(captured["request"]["input"])
    assert len(prompt["active_trade_context"]) == 2
    assert "deterministic lifecycle linker" in captured["request"]["instructions"]
