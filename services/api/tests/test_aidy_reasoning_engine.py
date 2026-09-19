"""The reasoning call: strict schema in, a typed annotation out, never a guess on failure.

Uses httpx.MockTransport rather than monkeypatching httpx.post -- the engine now issues
its request(s) through an httpx.AsyncClient (needed for the tool-calling round trips), so
the test seam is the injectable `transport` constructor arg, not the module-level function.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.aidy_reasoning_engine import (
    CALENDAR_TOOL_NAME,
    CALENDAR_TOOL_SCHEMA,
    CANDLE_TOOL_NAME,
    CANDLE_TOOL_SCHEMA,
    AidyReasoningEngine,
    AidyReasoningUnavailable,
    SignalContext,
)


def _context() -> SignalContext:
    return SignalContext(
        decision_id="d1",
        provider_name="Test Provider",
        side="BUY",
        symbol="XAUUSD",
        entry_low="2400.00",
        entry_high="2401.00",
        stop_loss="2390.00",
        take_profits=["2410.00", "2420.00"],
        decision_class="approve",
        decision_reasons=[{"code": "insufficient_track_record_evidence", "trades_resolved": 3}],
        trades_resolved=3,
    )


def _message_response(payload: dict, *, response_id: str = "resp_123") -> dict:
    return {
        "id": response_id,
        "usage": {"input_tokens": 250, "output_tokens": 80},
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(payload)}],
            }
        ],
    }


def _function_call_response(*, name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "id": "resp_tool_round",
        "usage": {"input_tokens": 300, "output_tokens": 40},
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
        ],
    }


def _scripted_transport(bodies: list[dict]) -> httpx.MockTransport:
    """Return the next scripted response body on each successive request."""
    remaining = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        if not remaining:
            raise AssertionError("no more scripted responses configured")
        return httpx.Response(200, json=remaining.pop(0), request=request)

    return httpx.MockTransport(handler)


_ANNOTATION = {
    "lean": "agree",
    "confidence": 0.7,
    "rationale": "Stop distance and reward:risk look disciplined.",
    "key_factors": ["R:R roughly 1:2", "stop on correct side of entry"],
    "shadow_action": "take",
    "risk_multiplier": 1.0,
    "action_reason": "The setup is coherent enough to take at configured risk.",
    "provider_claim_refs": [],
}


def test_a_well_formed_response_is_parsed_into_a_typed_annotation() -> None:
    engine = AidyReasoningEngine(
        api_key="test-key", transport=_scripted_transport([_message_response(_ANNOTATION)])
    )

    annotation = asyncio.run(engine.reason(_context()))

    assert annotation.lean == "agree"
    assert annotation.confidence == 0.7
    assert annotation.input_tokens == 250
    assert annotation.output_tokens == 80
    assert annotation.estimated_cost_usd > 0
    assert annotation.response_id == "resp_123"
    assert len(annotation.key_factors) == 2
    assert annotation.request_count == 1
    assert annotation.tool_calls_made == 0
    assert annotation.shadow_action == "take"
    assert annotation.risk_multiplier == 1.0


def test_an_invalid_lean_is_never_persisted_as_a_guess() -> None:
    bad = {**_ANNOTATION, "lean": "strongly_agree"}
    engine = AidyReasoningEngine(
        api_key="test-key", transport=_scripted_transport([_message_response(bad)])
    )

    with pytest.raises(AidyReasoningUnavailable):
        asyncio.run(engine.reason(_context()))


def test_a_malformed_upstream_response_raises_rather_than_fabricates() -> None:
    engine = AidyReasoningEngine(
        api_key="test-key", transport=_scripted_transport([{"output": []}])
    )

    with pytest.raises(AidyReasoningUnavailable):
        asyncio.run(engine.reason(_context()))


def test_an_http_error_is_retryable_not_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server_error"}, request=request)

    engine = AidyReasoningEngine(api_key="test-key", transport=httpx.MockTransport(handler))

    with pytest.raises(AidyReasoningUnavailable):
        asyncio.run(engine.reason(_context()))


def test_api_key_is_required() -> None:
    with pytest.raises(ValueError):
        AidyReasoningEngine(api_key="")


def test_market_context_is_sent_when_present_and_omitted_as_null_when_absent() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_message_response(_ANNOTATION), request=request)

    engine = AidyReasoningEngine(api_key="test-key", transport=httpx.MockTransport(handler))

    asyncio.run(engine.reason(_context()))
    sent_without_context = json.loads(captured[-1]["input"][0]["content"])
    assert sent_without_context["market_context"] is None

    with_context = replace(
        _context(),
        market_context={"trend_structure": "bullish_trend", "session": "london"},
    )
    asyncio.run(engine.reason(with_context))
    sent_with_context = json.loads(captured[-1]["input"][0]["content"])
    assert sent_with_context["market_context"] == {
        "trend_structure": "bullish_trend",
        "session": "london",
    }


def test_no_tool_executor_never_offers_the_candle_tool() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_message_response(_ANNOTATION), request=request)

    engine = AidyReasoningEngine(api_key="test-key", transport=httpx.MockTransport(handler))

    asyncio.run(engine.reason(_context(), tool_executor=None))

    assert len(captured) == 1
    assert "tools" not in captured[0]


def test_a_tool_call_is_executed_and_its_result_fed_back() -> None:
    tool_call = _function_call_response(
        name=CANDLE_TOOL_NAME, arguments={"timeframe_minutes": 15, "lookback_count": 5}
    )
    engine = AidyReasoningEngine(
        api_key="test-key",
        transport=_scripted_transport([tool_call, _message_response(_ANNOTATION)]),
    )
    executor_calls: list[tuple[str, dict]] = []

    async def executor(name: str, arguments: dict) -> dict:
        executor_calls.append((name, arguments))
        return {"timeframe_minutes": 15, "candles": [{"close": "2405.00"}]}

    annotation = asyncio.run(
        engine.reason(_context(), tool_executor=executor, tool_schemas=[CANDLE_TOOL_SCHEMA])
    )

    assert executor_calls == [
        (CANDLE_TOOL_NAME, {"timeframe_minutes": 15, "lookback_count": 5})
    ]
    assert annotation.lean == "agree"
    assert annotation.request_count == 2
    assert annotation.tool_calls_made == 1
    # Cost/tokens are summed across both rounds, not just the final one.
    assert annotation.input_tokens == 300 + 250
    assert annotation.output_tokens == 40 + 80


def test_tool_use_is_bounded_and_the_final_round_never_offers_tools() -> None:
    call_1 = _function_call_response(
        name=CANDLE_TOOL_NAME, arguments={"timeframe_minutes": 5, "lookback_count": 3}, call_id="c1"
    )
    call_2 = _function_call_response(
        name=CANDLE_TOOL_NAME,
        arguments={"timeframe_minutes": 15, "lookback_count": 3},
        call_id="c2",
    )
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests_seen.append(body)
        if len(requests_seen) == 1:
            return httpx.Response(200, json=call_1, request=request)
        if len(requests_seen) == 2:
            return httpx.Response(200, json=call_2, request=request)
        # Third (final, no-tools-offered) round must get a real answer.
        assert "tools" not in body
        return httpx.Response(200, json=_message_response(_ANNOTATION), request=request)

    engine = AidyReasoningEngine(api_key="test-key", transport=httpx.MockTransport(handler))

    async def executor(name: str, arguments: dict) -> dict:
        return {"ok": True}

    annotation = asyncio.run(
        engine.reason(_context(), tool_executor=executor, tool_schemas=[CANDLE_TOOL_SCHEMA])
    )

    assert len(requests_seen) == 3
    assert requests_seen[0].get("tools") and requests_seen[1].get("tools")
    assert annotation.request_count == 3
    assert annotation.tool_calls_made == 2


def test_a_failed_tool_fetch_result_is_still_fed_back_not_raised() -> None:
    """The tool executor itself decides how to represent a failure (an {"error": ...} dict,
    per aidy_reasoning_market_tools.py) -- the engine's job is only to pass it through."""
    tool_call = _function_call_response(
        name=CANDLE_TOOL_NAME, arguments={"timeframe_minutes": 5, "lookback_count": 3}
    )
    engine = AidyReasoningEngine(
        api_key="test-key",
        transport=_scripted_transport([tool_call, _message_response(_ANNOTATION)]),
    )

    async def failing_executor(name: str, arguments: dict) -> dict:
        return {"error": "candle_fetch_failed:TimeoutError"}

    annotation = asyncio.run(
        engine.reason(_context(), tool_executor=failing_executor, tool_schemas=[CANDLE_TOOL_SCHEMA])
    )

    assert annotation.lean == "agree"
    assert annotation.tool_calls_made == 1


def test_multiple_tools_can_be_offered_and_the_model_may_call_either() -> None:
    tool_call = _function_call_response(
        name=CALENDAR_TOOL_NAME,
        arguments={"hours_before": 6, "hours_after": 24, "min_impact": "high"},
    )
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests_seen.append(body)
        if len(requests_seen) == 1:
            return httpx.Response(200, json=tool_call, request=request)
        return httpx.Response(200, json=_message_response(_ANNOTATION), request=request)

    engine = AidyReasoningEngine(api_key="test-key", transport=httpx.MockTransport(handler))
    calls: list[tuple[str, dict]] = []

    async def executor(name: str, arguments: dict) -> dict:
        calls.append((name, arguments))
        return {"events": []}

    asyncio.run(
        engine.reason(
            _context(),
            tool_executor=executor,
            tool_schemas=[CANDLE_TOOL_SCHEMA, CALENDAR_TOOL_SCHEMA],
        )
    )

    assert calls == [
        (CALENDAR_TOOL_NAME, {"hours_before": 6, "hours_after": 24, "min_impact": "high"})
    ]
    offered_names = {tool["name"] for tool in requests_seen[0]["tools"]}
    assert offered_names == {CANDLE_TOOL_NAME, CALENDAR_TOOL_NAME}


def test_no_schemas_with_an_executor_still_never_offers_tools() -> None:
    """An executor alone can't honour a call without the model ever being told the tool
    exists -- both tool_executor and tool_schemas must be present for tools to be offered."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_message_response(_ANNOTATION), request=request)

    engine = AidyReasoningEngine(api_key="test-key", transport=httpx.MockTransport(handler))

    async def executor(name: str, arguments: dict) -> dict:
        raise AssertionError("must never be called")

    asyncio.run(engine.reason(_context(), tool_executor=executor, tool_schemas=None))

    assert len(captured) == 1
    assert "tools" not in captured[0]


def test_system_instructions_require_candles_for_ambiguous_or_countertrend_structure() -> None:
    from app import aidy_reasoning_engine as module

    instructions = " ".join(module._SYSTEM_INSTRUCTIONS.split())
    assert "trend_structure is mixed/range/unknown" in instructions
    assert "when the signal runs against a clear multi-timeframe trend" in instructions
    assert "Do not call it merely to" in instructions


def test_valid_provider_claim_ref_is_preserved() -> None:
    claim = {
        "id": "provider.performance.side.SELL",
        "kind": "provider_side_performance",
        "source": "provider_profile",
        "path": "provider_profile.performance.side_buckets.SELL",
        "value": {"trades": 20, "wins": 15, "losses": 5, "win_rate_percent": 75.0},
        "sample_n": 20,
        "version": 12,
        "as_of_utc": "2026-09-18T12:00:00+00:00",
    }
    context = replace(_context(), provider_evidence_claims=[claim])
    payload = {**_ANNOTATION, "provider_claim_refs": [claim["id"]]}
    engine = AidyReasoningEngine(
        api_key="test-key", transport=_scripted_transport([_message_response(payload)])
    )

    annotation = asyncio.run(engine.reason(context))

    assert annotation.provider_claim_refs == ("provider.performance.side.SELL",)


def test_model_cannot_reference_provider_evidence_that_does_not_exist() -> None:
    payload = {**_ANNOTATION, "provider_claim_refs": ["provider.performance.side.BUY"]}
    engine = AidyReasoningEngine(
        api_key="test-key", transport=_scripted_transport([_message_response(payload)])
    )

    with pytest.raises(AidyReasoningUnavailable, match="aidy_reasoning_provider_claim_invalid"):
        asyncio.run(engine.reason(_context()))


def test_provider_history_cannot_leak_into_free_text() -> None:
    claim = {
        "id": "provider.performance.side.SELL",
        "kind": "provider_side_performance",
        "source": "provider_profile",
        "path": "provider_profile.performance.side_buckets.SELL",
        "value": {"trades": 20, "wins": 15, "losses": 5, "win_rate_percent": 75.0},
        "sample_n": 20,
        "version": 12,
        "as_of_utc": "2026-09-18T12:00:00+00:00",
    }
    context = replace(_context(), provider_evidence_claims=[claim])
    payload = {
        **_ANNOTATION,
        "rationale": "The provider is historically stronger on SELL.",
        "provider_claim_refs": [claim["id"]],
    }
    engine = AidyReasoningEngine(
        api_key="test-key", transport=_scripted_transport([_message_response(payload)])
    )

    with pytest.raises(AidyReasoningUnavailable, match="aidy_reasoning_provider_claim_invalid"):
        asyncio.run(engine.reason(context))


def test_prompt_sends_atomic_claims_not_raw_provider_history() -> None:
    captured: list[dict] = []
    claim = {
        "id": "provider.performance.side.SELL",
        "kind": "provider_side_performance",
        "source": "provider_profile",
        "path": "provider_profile.performance.side_buckets.SELL",
        "value": {"trades": 20, "wins": 15, "losses": 5, "win_rate_percent": 75.0},
        "sample_n": 20,
        "version": 12,
        "as_of_utc": "2026-09-18T12:00:00+00:00",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_message_response(_ANNOTATION), request=request)

    context = replace(
        _context(),
        provider_evidence_claims=[claim],
        provider_profile={"performance": {"invented": "must not leave the process"}},
        provider_intelligence={"governance": {"invented": "must not leave the process"}},
        provider_fingerprint_summary="legacy unstructured fingerprint must not be sent",
    )
    engine = AidyReasoningEngine(api_key="test-key", transport=httpx.MockTransport(handler))

    asyncio.run(engine.reason(context))

    sent = json.loads(captured[0]["input"][0]["content"])
    assert sent["provider_evidence_claims"] == [claim]
    assert "provider_profile" not in sent
    assert "provider_intelligence" not in sent
    assert "provider_fingerprint" not in sent
    assert "provider_name" not in sent
    assert "provider_trades_resolved" not in sent


def test_gold_state_v2_prompt_forbids_proxy_causality_and_prediction() -> None:
    from app import aidy_reasoning_engine as module

    instructions = " ".join(module._SYSTEM_INSTRUCTIONS.split())
    assert "Gold State v2 is DESCRIPTIVE, not a directional prediction" in instructions
    assert "proxy_not_order_flow=true" in instructions
    assert "never call this hidden orders" in instructions
    assert "causal_attribution_proven=false" in instructions
    assert "When cause_unknown=true, explicitly treat the cause as unknown" in instructions
    assert "proximity is risk context, never proof that the event caused any move" in instructions
