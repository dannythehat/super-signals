"""The reasoning call: strict schema in, a typed annotation out, never a guess on failure."""

from __future__ import annotations

import json

import httpx
import pytest

from app.aidy_reasoning_engine import (
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


def _fake_response(payload: dict) -> httpx.Response:
    body = {
        "id": "resp_123",
        "usage": {"input_tokens": 250, "output_tokens": 80},
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps(payload)}],
            }
        ],
    }
    return httpx.Response(200, json=body, request=httpx.Request("POST", "https://x.test"))


def test_a_well_formed_response_is_parsed_into_a_typed_annotation(monkeypatch) -> None:
    def fake_post(url, *, headers, json, timeout):  # noqa: A002 - matches httpx.post signature
        return _fake_response(
            {
                "lean": "agree",
                "confidence": 0.7,
                "rationale": "Stop distance and reward:risk look disciplined.",
                "key_factors": ["R:R roughly 1:2", "stop on correct side of entry"],
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    engine = AidyReasoningEngine(api_key="test-key")

    annotation = engine.reason(_context())

    assert annotation.lean == "agree"
    assert annotation.confidence == 0.7
    assert annotation.input_tokens == 250
    assert annotation.output_tokens == 80
    assert annotation.estimated_cost_usd > 0
    assert annotation.response_id == "resp_123"
    assert len(annotation.key_factors) == 2


def test_an_invalid_lean_is_never_persisted_as_a_guess(monkeypatch) -> None:
    def fake_post(url, *, headers, json, timeout):  # noqa: A002
        return _fake_response(
            {
                "lean": "strongly_agree",  # not in the strict enum
                "confidence": 0.9,
                "rationale": "x",
                "key_factors": [],
            }
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    engine = AidyReasoningEngine(api_key="test-key")

    with pytest.raises(AidyReasoningUnavailable):
        engine.reason(_context())


def test_a_malformed_upstream_response_raises_rather_than_fabricates(monkeypatch) -> None:
    def fake_post(url, *, headers, json, timeout):  # noqa: A002
        return httpx.Response(200, json={"output": []}, request=httpx.Request("POST", "https://x.test"))

    monkeypatch.setattr(httpx, "post", fake_post)
    engine = AidyReasoningEngine(api_key="test-key")

    with pytest.raises(AidyReasoningUnavailable):
        engine.reason(_context())


def test_an_http_error_is_retryable_not_terminal(monkeypatch) -> None:
    def fake_post(url, *, headers, json, timeout):  # noqa: A002
        request = httpx.Request("POST", "https://x.test")
        return httpx.Response(500, json={"error": "server_error"}, request=request)

    monkeypatch.setattr(httpx, "post", fake_post)
    engine = AidyReasoningEngine(api_key="test-key")

    with pytest.raises(AidyReasoningUnavailable):
        engine.reason(_context())


def test_api_key_is_required() -> None:
    with pytest.raises(ValueError):
        AidyReasoningEngine(api_key="")
