"""Fast structured AI interpretation for Telegram provider messages.

The model's only job is to identify what the provider explicitly instructed.
It never decides whether a trade is attractive and never invents missing prices.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx


class AiMessageGatewayError(RuntimeError):
    """Sanitized AI gateway failure suitable for deterministic fallback."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AiGatewayDecision:
    output: dict[str, Any]
    model: str
    latency_ms: int


_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["new_trade", "trade_update", "preparation", "chatter", "skip"],
        },
        "reason": {"type": "string"},
        "symbol": {"type": ["string", "null"]},
        "direction": {
            "type": ["string", "null"],
            "enum": ["BUY", "SELL", None],
        },
        "order_type": {
            "type": ["string", "null"],
            "enum": ["market", "limit", "stop", "unknown", None],
        },
        "entries": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
        },
        "stop_loss": {"type": ["string", "null"]},
        "take_profits": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        },
        "open_runner": {"type": "boolean"},
        "double_lot": {"type": "boolean"},
        "update_action": {
            "type": "string",
            "enum": [
                "none",
                "tp_hit",
                "sl_hit",
                "move_sl",
                "break_even",
                "close",
                "partial_close",
                "cancel",
                "hold",
                "other",
            ],
        },
        "target_index": {"type": ["integer", "null"], "minimum": 1, "maximum": 20},
        "new_stop_loss": {"type": ["string", "null"]},
        "stated_pips": {"type": ["string", "null"]},
    },
    "required": [
        "decision",
        "reason",
        "symbol",
        "direction",
        "order_type",
        "entries",
        "stop_loss",
        "take_profits",
        "open_runner",
        "double_lot",
        "update_action",
        "target_index",
        "new_stop_loss",
        "stated_pips",
    ],
}

_INSTRUCTIONS = """You are the Super Signals Telegram message interpreter.

You MUST always return one decision for the supplied provider message. Your job is only to identify what the provider explicitly instructed. You are NOT a trading adviser and must never decide whether a trade is good, improve a price, choose a stop, choose a target, or invent a missing value.

Decision meanings:
- new_trade: an actual new BUY/SELL instruction with trade fields.
- trade_update: management/result instruction for an existing trade, including TP hit, SL hit, move SL, break even, close, partial close, cancel, hold, or stated pips.
- preparation: advance warning such as PREPARE FOR A BUY, WATCH FOR SELL, setup coming, with no executable trade yet.
- chatter: greetings, marketing, celebration, market commentary or other non-actionable text.
- skip: trade-looking text whose meaning cannot be safely identified from what was actually written.

Extraction rules:
- GOLD is the provider label for XAUUSD; return symbol XAUUSD when GOLD clearly names the traded instrument.
- Preserve every explicitly stated entry. A range such as 4371/4366 or 4371-4366 must return both values in entries. 'Entry' plus 'Second entry' must also return both. Do not collapse them.
- BUY LIMIT / BUY LIMITS / SELL LIMIT / SELL LIMITS means order_type limit. BUY STOP / SELL STOP means order_type stop. BUY NOW / SELL NOW or a plain BUY/SELL instruction with an entry is market unless pending wording is explicit.
- Return only numeric TP values in take_profits. If the message explicitly says TP OPEN or equivalent open runner, set open_runner true instead of inventing a TP price.
- Set double_lot true ONLY when the message explicitly says double lot/double lot size or an unambiguous equivalent instruction to double the lot. HIGH RISK TRADE alone is NOT double lot.
- For TP hit messages, set update_action tp_hit and target_index when stated. For SL hit use sl_hit. For move-SL instructions extract new_stop_loss only if a numeric new SL is explicitly stated. Break-even can have no numeric new_stop_loss.
- stated_pips is only a number explicitly claimed in the message, never calculated by you.
- If a value is not explicitly present, return null or an empty array as appropriate.
- Ignore emojis and formatting noise while preserving the provider's actual instruction.
"""


class OpenAiMessageSupervisorGateway:
    """Call OpenAI Responses API using strict JSON-schema output."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")
        if not self._api_key:
            raise ValueError("openai_api_key_missing")
        if not self._model:
            raise ValueError("ai_supervisor_model_missing")

    def decide(
        self,
        *,
        raw_text: str,
        source_alias: str,
        reply_text: str | None,
    ) -> AiGatewayDecision:
        user_payload = {
            "source_alias": source_alias,
            "message": raw_text,
            "reply_context": reply_text or None,
        }
        body = {
            "model": self._model,
            "store": False,
            "instructions": _INSTRUCTIONS,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(user_payload, ensure_ascii=False),
                        }
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "super_signals_message_decision",
                    "description": "Immediate structured interpretation of one Telegram provider message.",
                    "strict": True,
                    "schema": _DECISION_SCHEMA,
                }
            },
            "max_output_tokens": 1200,
        }

        started = perf_counter()
        try:
            with httpx.Client(timeout=float(self._timeout_seconds)) as client:
                response = client.post(
                    f"{self._base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AiMessageGatewayError("ai_supervisor_unavailable") from exc

        latency_ms = max(0, int((perf_counter() - started) * 1000))
        if response.status_code >= 500 or response.status_code == 429:
            raise AiMessageGatewayError("ai_supervisor_unavailable")
        if response.status_code >= 400:
            raise AiMessageGatewayError("ai_supervisor_request_rejected")

        try:
            payload = response.json()
            output_text = self._output_text(payload)
            parsed = json.loads(output_text)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AiMessageGatewayError("ai_supervisor_invalid_response") from exc
        if not isinstance(parsed, dict):
            raise AiMessageGatewayError("ai_supervisor_invalid_response")
        return AiGatewayDecision(output=parsed, model=self._model, latency_ms=latency_ms)

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        for item in payload.get("output", []) or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return str(content["text"])
        raise ValueError("response_output_text_missing")
