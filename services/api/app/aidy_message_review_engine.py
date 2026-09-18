"""AIDY research-only second opinion on messages the upstream interpreter skipped.

The output never changes parser output, signal rows, positions or broker state. It exists
only to discover provider-specific language/sequence patterns the deterministic/live path
may be missing so future parser rules can be improved from evidence rather than guesses.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

MODEL_VERSION = "aidy_message_review_v1"
PROMPT_VERSION = "aidy_message_review_prompt_v1"
_DEFAULT_MODEL = "gpt-5-mini-2025-08-07"

_USD_PER_INPUT_TOKEN = Decimal("0.00000025")
_USD_PER_OUTPUT_TOKEN = Decimal("0.000002")

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "review_class": {
            "type": "string",
            "enum": ["likely_trade", "likely_management", "correct_skip", "uncertain"],
        },
        "suggested_action": {
            "type": "string",
            "enum": [
                "execute_candidate",
                "management_candidate",
                "ignore",
                "needs_rule_review",
            ],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "missing_fields": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
        "rationale": {"type": "string"},
        "suggested_parser_rule": {"type": "string"},
    },
    "required": [
        "review_class",
        "suggested_action",
        "confidence",
        "missing_fields",
        "rationale",
        "suggested_parser_rule",
    ],
}

_SYSTEM_INSTRUCTIONS = """You are AIDY reviewing one Telegram provider message that the
production interpreter did NOT execute. This is a research-only second opinion. You have zero broker authority,
and your answer must never be treated as permission to trade.

You are given the exact current message, the original interpreter decision/action/reason,
the provider's point-in-time profile, and a few messages from the same provider posted no later
than the current message. Decide whether the skip/ignore looks correct or whether the message is
likely part of a trade or management sequence that deserves a provider-specific parser rule.

Never invent a side, entry, stop, target or management value. Older messages are context only:
you may say a direct-reply/edit sequence appears linked, but do not transplant an old numeric
level into the current message unless the current message/reply relationship explicitly makes
that linkage part of the provider's instruction.

review_class=likely_trade means the current message likely represents/activates a new trade that
the interpreter missed. likely_management means it likely manages an already-open provider trade.
correct_skip means the interpreter was right to leave it alone. uncertain means evidence is not
strong enough. execute_candidate/management_candidate are research labels only, never execution.
If the original interpreter reason says the side, stop loss, instrument or entry is missing, you
must never output execute_candidate. Use needs_rule_review instead, even when review_class is
likely_trade, and list the missing field explicitly.

missing_fields lists information genuinely absent from the current evidence. suggested_parser_rule
must be a concise, provider-specific engineering hypothesis, or an empty string when no rule
change is warranted. Keep rationale short and evidence-based.
"""


@dataclass(frozen=True, slots=True)
class MessageReviewContext:
    observation_id: str
    provider_name: str
    observed_at: str
    current_message: str
    original_decision: str
    original_action: str
    original_outcome_reason: str | None
    provider_profile: dict[str, Any] | None
    recent_messages: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class MessageReview:
    review_class: str
    suggested_action: str
    confidence: float
    missing_fields: list[str]
    rationale: str
    suggested_parser_rule: str
    model_name: str
    response_id: str | None
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal
    latency_ms: int


class AidyMessageReviewUnavailable(RuntimeError):
    pass


class AidyMessageReviewEngine:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        timeout_seconds: int = 15,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("openai_api_key_missing")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def review(self, context: MessageReviewContext) -> MessageReview:
        started = time.perf_counter()
        payload = {
            "model": self._model,
            "store": False,
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": 500,
            "instructions": _SYSTEM_INSTRUCTIONS,
            "input": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "observation_id": context.observation_id,
                            "provider_name": context.provider_name,
                            "observed_at": context.observed_at,
                            "current_message": context.current_message,
                            "original_decision": context.original_decision,
                            "original_action": context.original_action,
                            "original_outcome_reason": context.original_outcome_reason,
                            "provider_profile": context.provider_profile,
                            "recent_messages": context.recent_messages,
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "aidy_message_review",
                    "strict": True,
                    "schema": REVIEW_SCHEMA,
                }
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(
                    f"{self._base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()

            output_text = None
            for item in body.get("output", []):
                if item.get("type") != "message":
                    continue
                for part in item.get("content", []):
                    if part.get("type") == "output_text" and part.get("text"):
                        output_text = str(part["text"])
                        break
            if not output_text:
                raise AidyMessageReviewUnavailable("aidy_message_review_output_missing")
            parsed = json.loads(output_text)
        except AidyMessageReviewUnavailable:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise AidyMessageReviewUnavailable("aidy_message_review_unavailable") from exc

        review_class = str(parsed["review_class"])
        suggested_action = str(parsed["suggested_action"])
        if review_class not in {"likely_trade", "likely_management", "correct_skip", "uncertain"}:
            raise AidyMessageReviewUnavailable("aidy_message_review_class_invalid")
        if suggested_action not in {
            "execute_candidate",
            "management_candidate",
            "ignore",
            "needs_rule_review",
        }:
            raise AidyMessageReviewUnavailable("aidy_message_review_action_invalid")
        confidence = float(parsed["confidence"])
        if not 0 <= confidence <= 1:
            raise AidyMessageReviewUnavailable("aidy_message_review_confidence_invalid")

        missing_fields = [str(x)[:100] for x in parsed["missing_fields"]][:8]
        critical_missing = {
            "missing_side": "side",
            "missing_sl": "stop_loss",
            "missing_instrument": "instrument",
            "missing_entry": "entry",
        }
        required_field = critical_missing.get(context.original_outcome_reason or "")
        if required_field is not None:
            if required_field not in missing_fields:
                missing_fields = [*missing_fields, required_field][:8]
            if suggested_action == "execute_candidate":
                suggested_action = "needs_rule_review"

        usage = body.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cost = (
            Decimal(input_tokens) * _USD_PER_INPUT_TOKEN
            + Decimal(output_tokens) * _USD_PER_OUTPUT_TOKEN
        )
        return MessageReview(
            review_class=review_class,
            suggested_action=suggested_action,
            confidence=confidence,
            missing_fields=missing_fields,
            rationale=str(parsed["rationale"])[:2000],
            suggested_parser_rule=str(parsed["suggested_parser_rule"])[:1000],
            model_name=self._model,
            response_id=str(body.get("id")) if body.get("id") else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


__all__ = [
    "MODEL_VERSION",
    "PROMPT_VERSION",
    "AidyMessageReviewEngine",
    "AidyMessageReviewUnavailable",
    "MessageReview",
    "MessageReviewContext",
]
