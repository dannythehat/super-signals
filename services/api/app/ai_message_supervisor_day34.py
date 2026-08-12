"""Day 34 active-trade-aware AI interpretation.

This layer keeps the accepted AI Message Supervisor contract intact while adding one
new semantic input: broker-backed active trade context for the SAME Telegram source.
The context helps the model recognise terse provider management messages while an
actual mapped position is still active. It never supplies execution evidence for a
new trade and never weakens Day 27 fail-closed targeting.
"""

from __future__ import annotations

import json
import time
from hashlib import sha256
from typing import Any

import httpx

from app.ai_message_supervisor import (
    AI_DECISION_SCHEMA,
    AiMessageDecision,
    AiSupervisorError,
    OpenAiMessageSupervisor,
    _SYSTEM_INSTRUCTIONS,
    _guard_execute_decision,
    _guard_provider_intent,
)

_DAY34_ACTIVE_TRADE_INSTRUCTIONS = _SYSTEM_INSTRUCTIONS + """

ACTIVE TRADE WATCH — DAY 34
You may also receive active_trade_context. It contains ONLY broker-backed active trade
state belonging to this same logical Telegram source. When it is non-empty, do not
interpret the current message as an isolated sentence. Explicitly consider whether the
provider is managing, reporting, closing, cancelling or changing one of those active
trades.

Terse provider wording can still be a real trade update. Examples include 'BE now',
'move to BE', 'close gold', 'close all', 'secure here', 'TP1 done', 'SL hit', 'out now',
'cancel it', or an explicit new SL/TP value when the surrounding source-specific
context makes the management meaning clear. Do not classify a terse message as chatter
merely because the full original setup is not repeated.

active_trade_context is for SEMANTIC UNDERSTANDING AND TARGET LINKING ONLY. It is not
permission to invent a management instruction, infer an unstated numeric value, create
a new trade, or import old entry/SL/TP values as evidence for a new execution. The
current Telegram message must still explicitly instruct the management action. For new
trade execution, the existing current-message/direct-reply evidence rule remains
unchanged.

If the current message clearly is trade management but more than one active trade in
the same source could plausibly be the target, classify it as trade_update with
apply_update when the management intent itself is explicit, but do not guess a signal
identity. The deterministic lifecycle linker and Day 27 broker gate will stop an
ambiguous target. Never choose the newest, closest-priced or most profitable trade as a
discretionary tie-breaker.

Broker truth controls membership in active_trade_context. A settled trade is historical
context only and must not be treated as still open merely because an old Telegram post
exists. Telegram/provider wording can add meaning, but it cannot override contradictory
broker state.
"""


class Day34OpenAiMessageSupervisor(OpenAiMessageSupervisor):
    """OpenAI supervisor with explicit same-source broker Active Trade Watch context."""

    def decide_with_active_context(
        self,
        *,
        raw_text: str,
        source_status: str,
        active_trade_context: list[dict[str, Any]],
        source_name: str | None = None,
        recent_source_messages: list[dict[str, Any]] | None = None,
        reply_context: str | None = None,
        previous_text: str | None = None,
        is_edit: bool = False,
    ) -> AiMessageDecision:
        started = time.perf_counter()
        prompt = {
            "source_name": source_name,
            "source_status": source_status,
            "active_trade_context": active_trade_context,
            "recent_source_messages": recent_source_messages or [],
            "telegram_message": raw_text,
            "reply_context": reply_context,
            "is_edit": is_edit,
            "previous_text": previous_text,
        }
        payload = {
            "model": self._model,
            "store": False,
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": 1200,
            "instructions": _DAY34_ACTIVE_TRADE_INSTRUCTIONS,
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

        parsed = _guard_provider_intent(parsed, raw_text, is_edit=is_edit)
        parsed = _guard_execute_decision(parsed, raw_text, linked_context=reply_context)
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


__all__ = ["Day34OpenAiMessageSupervisor"]
