from __future__ import annotations

import asyncio
import json

import httpx

from app.aidy_message_review_engine import (
    AidyMessageReviewEngine,
    MessageReviewContext,
)


def _context() -> MessageReviewContext:
    return MessageReviewContext(
        observation_id="obs-1",
        provider_name="Test Provider",
        observed_at="2026-09-18T12:00:00+00:00",
        current_message="Close half and move SL to entry",
        original_decision="trade_update",
        original_action="ignore",
        original_outcome_reason="unsupported_management",
        provider_profile={"interpretation_readiness": 0.8},
        recent_messages=[{"text": "BUY GOLD 4300 SL 4290 TP 4310"}],
    )


def test_message_review_is_strict_research_second_opinion() -> None:
    payload = {
        "review_class": "likely_management",
        "suggested_action": "management_candidate",
        "confidence": 0.91,
        "missing_fields": [],
        "rationale": "Explicit close-half and breakeven management language.",
        "suggested_parser_rule": "Recognise this provider's 'move SL to entry' phrasing.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["store"] is False
        assert "zero broker authority" in body["instructions"]
        return httpx.Response(
            200,
            json={
                "id": "resp-review",
                "usage": {"input_tokens": 100, "output_tokens": 40},
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(payload)}
                        ],
                    }
                ],
            },
            request=request,
        )

    engine = AidyMessageReviewEngine(
        api_key="test", transport=httpx.MockTransport(handler)
    )
    review = asyncio.run(engine.review(_context()))

    assert review.review_class == "likely_management"
    assert review.suggested_action == "management_candidate"
    assert review.confidence == 0.91
    assert review.response_id == "resp-review"
    assert review.estimated_cost_usd > 0


def test_missing_side_can_never_be_execute_candidate() -> None:
    payload = {
        "review_class": "likely_trade",
        "suggested_action": "execute_candidate",
        "confidence": 0.88,
        "missing_fields": [],
        "rationale": "Provider-specific compact message may be trade-related.",
        "suggested_parser_rule": "Review the compact provider pattern.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-missing-side",
                "usage": {"input_tokens": 80, "output_tokens": 30},
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(payload)}
                        ],
                    }
                ],
            },
            request=request,
        )

    context = _context()
    context = MessageReviewContext(
        observation_id=context.observation_id,
        provider_name=context.provider_name,
        observed_at=context.observed_at,
        current_message="4381💥💥",
        original_decision="new_trade",
        original_action="skip",
        original_outcome_reason="missing_side",
        provider_profile=context.provider_profile,
        recent_messages=context.recent_messages,
    )
    engine = AidyMessageReviewEngine(
        api_key="test", transport=httpx.MockTransport(handler)
    )
    review = asyncio.run(engine.review(context))

    assert review.review_class == "likely_trade"
    assert review.suggested_action == "needs_rule_review"
    assert "side" in review.missing_fields
