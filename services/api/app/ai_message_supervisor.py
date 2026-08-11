"""Immediate AI interpretation for Telegram source messages and edits.

The supervisor answers only what the provider explicitly instructed. It never invents
entry, stop loss, take profit, sizing, or an exit. Every call returns a strict
machine-readable decision so the live path never waits for human review.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

import httpx


AI_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["new_trade", "trade_update", "chatter", "preparation", "non_actionable"],
        },
        "action": {
            "type": "string",
            "enum": ["execute", "apply_update", "ignore", "skip"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
        "symbol": {"type": ["string", "null"]},
        "side": {"type": ["string", "null"], "enum": ["BUY", "SELL", None]},
        "order_type": {
            "type": ["string", "null"],
            "enum": ["market", "pending", None],
        },
        "entry_low": {"type": ["string", "null"]},
        "entry_high": {"type": ["string", "null"]},
        "stop_loss": {"type": ["string", "null"]},
        "take_profits": {"type": "array", "items": {"type": "string"}},
        "double_lot": {"type": "boolean"},
        "update_type": {
            "type": ["string", "null"],
            "enum": [
                "tp_hit",
                "close",
                "close_half",
                "move_to_break_even",
                "edit_stop_loss",
                "edit_take_profit",
                "cancel_pending",
                "result_report",
                "other",
                None,
            ],
        },
        "update_target": {"type": ["string", "null"]},
        "update_value": {"type": ["string", "null"]},
        "provider_claimed_pips": {"type": ["string", "null"]},
    },
    "required": [
        "decision",
        "action",
        "confidence",
        "reason",
        "symbol",
        "side",
        "order_type",
        "entry_low",
        "entry_high",
        "stop_loss",
        "take_profits",
        "double_lot",
        "update_type",
        "update_target",
        "update_value",
        "provider_claimed_pips",
    ],
}

_SYSTEM_INSTRUCTIONS = """You are the Super Signals Telegram message interpreter.
Your only job is to determine what the signal provider explicitly instructed.
Never make a trading recommendation. Never decide whether a setup is good or bad.
Never invent, infer, improve, adjust, or substitute a numeric entry, stop loss,
take profit, lot size, or exit that is not explicitly present in the supplied
message/context.

Classify every message immediately:
- new_trade: an instruction to open/place a trade
- trade_update: an instruction/result relating to an existing signal
- chatter: conversation, celebration, marketing, commentary, greetings
- preparation: heads-up/watch/wait/get-ready language with no executable instruction
- non_actionable: incomplete/unsupported/unclear instruction

Use action=execute only for a mechanically complete new trade. Use apply_update only
for an explicit trade-management/result instruction. Use ignore for chatter or
preparation. Use skip for incomplete/unsupported/unclear instructions.

Treat GOLD as XAUUSD when the provider is clearly referring to gold. Preserve entry
ranges as entry_low/entry_high. If only one exact entry is supplied, set both to that
same value. BUY LIMIT/SELL LIMIT are pending orders. 'High risk' is not double lot.
Only explicit double/double lot wording sets double_lot=true.

When is_edit=true, the Telegram message is a revision of the same provider message,
not a second trade. Compare previous_text with telegram_message. If the edit changes
an existing signal instruction, classify it as trade_update/action=apply_update and
return the full explicit revised trade fields plus the most specific update_type you
can identify. If there is no actionable instruction change, ignore or skip it. Never
turn an edit into a duplicate new trade.

Return only the requested structured object."""


@dataclass(frozen=True, slots=True)
class AiMessageDecision:
    decision: str
    action: str
    confidence: float
    reason: str
    extracted: dict[str, Any]
    model: str
    response_id: str | None
    latency_ms: int
    source: str
    raw_text_sha256: str


class AiSupervisorError(RuntimeError):
    pass


class OpenAiMessageSupervisor:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int = 6,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        if not api_key:
            raise ValueError("openai_api_key_missing")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")

    def decide(
        self,
        *,
        raw_text: str,
        source_status: str,
        reply_context: str | None = None,
        previous_text: str | None = None,
        is_edit: bool = False,
    ) -> AiMessageDecision:
        started = time.perf_counter()
        prompt = {
            "source_status": source_status,
            "telegram_message": raw_text,
            "reply_context": reply_context,
            "is_edit": is_edit,
            "previous_text": previous_text,
        }
        payload = {
            "model": self._model,
            "store": False,
            "instructions": _SYSTEM_INSTRUCTIONS,
            "input": json.dumps(prompt, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "super_signals_message_decision",
                    "strict": True,
                    "schema": AI_DECISION_SCHEMA,
                }
            },
        }
        try:
            response = httpx.post(
                f"{self._base_url}/responses",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            parsed = json.loads(self._output_text(body))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AiSupervisorError("ai_supervisor_unavailable") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        return AiMessageDecision(
            decision=str(parsed["decision"]),
            action=str(parsed["action"]),
            confidence=float(parsed["confidence"]),
            reason=str(parsed["reason"]),
            extracted={
                key: parsed[key]
                for key in (
                    "symbol",
                    "side",
                    "order_type",
                    "entry_low",
                    "entry_high",
                    "stop_loss",
                    "take_profits",
                    "double_lot",
                    "update_type",
                    "update_target",
                    "update_value",
                    "provider_claimed_pips",
                )
            },
            model=self._model,
            response_id=(str(body.get("id")) if body.get("id") else None),
            latency_ms=latency_ms,
            source="openai",
            raw_text_sha256=sha256(raw_text.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _output_text(body: dict[str, Any]) -> str:
        for item in body.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"])
        raise AiSupervisorError("ai_supervisor_output_missing")
