"""Day 34 active-trade-aware AI interpretation.

This layer keeps the accepted AI Message Supervisor contract intact while adding one
new semantic input: broker-backed active trade context for the SAME Telegram source.
The context helps the model recognise terse provider management messages while an
actual mapped position is still active. It never supplies execution evidence for a
new trade and never weakens Day 27 fail-closed targeting.
"""

from __future__ import annotations

import json
import os
import time
from hashlib import sha256
from typing import Any

import httpx
from sqlalchemy import text

from app.ai_cost_prefilter import deterministic_ai_cost_prefilter
from app.ai_message_supervisor import (
    AI_DECISION_SCHEMA,
    AiMessageDecision,
    AiSupervisorError,
    OpenAiMessageSupervisor,
    _SYSTEM_INSTRUCTIONS,
    _guard_execute_decision,
    _guard_provider_intent,
)
from app.db import get_session_factory

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


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Persistent spend guard. These counts use the durable decision ledger, so a Render
# restart/redeploy cannot reset the allowance. Shadow analysis has its own much smaller
# pool so research can never consume the live/testing semantic allowance.
_PAID_AI_DAILY_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_DAILY_CALL_LIMIT", 200)
_PAID_AI_MONTHLY_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_MONTHLY_CALL_LIMIT", 4000)
_PAID_AI_SHADOW_DAILY_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_SHADOW_DAILY_CALL_LIMIT", 30)
_PAID_AI_SHADOW_MONTHLY_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_SHADOW_MONTHLY_CALL_LIMIT", 600)
_MAX_OUTPUT_TOKENS = _positive_int_env("SUPER_SIGNALS_AI_MAX_OUTPUT_TOKENS", 500)
_RECENT_CONTEXT_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_RECENT_CONTEXT_LIMIT", 8)
_RECENT_CONTEXT_TEXT_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_RECENT_TEXT_LIMIT", 500)
_ACTIVE_CONTEXT_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_ACTIVE_CONTEXT_LIMIT", 6)
_ACTIVE_LIFECYCLE_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_ACTIVE_LIFECYCLE_LIMIT", 3)
_ACTIVE_LIFECYCLE_TEXT_LIMIT = _positive_int_env("SUPER_SIGNALS_AI_ACTIVE_LIFECYCLE_TEXT_LIMIT", 250)


def _budget_skip(raw_text: str, reason: str) -> AiMessageDecision:
    return AiMessageDecision(
        decision="non_actionable",
        action="skip",
        confidence=1.0,
        reason=reason,
        extracted={
            "symbol": None,
            "side": None,
            "order_type": None,
            "entry_low": None,
            "entry_high": None,
            "stop_loss": None,
            "take_profits": [],
            "double_lot": False,
            "update_type": None,
            "update_target": None,
            "update_value": None,
            "provider_claimed_pips": None,
        },
        model="canonical-ai-budget-guard-v1",
        response_id=None,
        latency_ms=0,
        source="deterministic_no_ai",
        raw_text_sha256=sha256((raw_text or "").encode("utf-8")).hexdigest(),
    )


def _paid_ai_budget_reason(source_status: str) -> str | None:
    """Return a fail-closed reason when the persistent paid-AI allowance is exhausted."""
    try:
        factory = get_session_factory()
        with factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE d.created_at >= date_trunc('day', now())
                        ) AS day_total,
                        COUNT(*) AS month_total,
                        COUNT(*) FILTER (
                            WHERE d.created_at >= date_trunc('day', now())
                              AND s.status = 'shadow'
                        ) AS day_shadow,
                        COUNT(*) FILTER (
                            WHERE s.status = 'shadow'
                        ) AS month_shadow
                    FROM ai_message_decisions AS d
                    JOIN messages AS m ON m.id = d.message_id
                    JOIN sources AS s ON s.id = m.source_id
                    WHERE d.decision_source = 'openai'
                      AND d.created_at >= date_trunc('month', now())
                    """
                )
            ).mappings().one()
    except Exception:
        # A broken accounting check must never silently permit unlimited paid calls.
        return "paid_ai_budget_guard_unavailable"

    day_total = int(row["day_total"] or 0)
    month_total = int(row["month_total"] or 0)
    if day_total >= _PAID_AI_DAILY_LIMIT:
        return "paid_ai_daily_limit_reached"
    if month_total >= _PAID_AI_MONTHLY_LIMIT:
        return "paid_ai_monthly_limit_reached"

    if str(source_status or "").strip().lower() == "shadow":
        if int(row["day_shadow"] or 0) >= _PAID_AI_SHADOW_DAILY_LIMIT:
            return "paid_ai_shadow_daily_limit_reached"
        if int(row["month_shadow"] or 0) >= _PAID_AI_SHADOW_MONTHLY_LIMIT:
            return "paid_ai_shadow_monthly_limit_reached"
    return None


def _compact_recent_context(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in list(messages or [])[-_RECENT_CONTEXT_LIMIT:]:
        row = dict(item)
        row["text"] = str(row.get("text") or "")[:_RECENT_CONTEXT_TEXT_LIMIT]
        compact.append(row)
    return compact


def _compact_active_context(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in list(items or [])[:_ACTIVE_CONTEXT_LIMIT]:
        row = dict(item)
        lifecycle = []
        for event in list(row.get("recent_lifecycle") or [])[-_ACTIVE_LIFECYCLE_LIMIT:]:
            event_row = dict(event)
            event_row["rendered_text"] = str(event_row.get("rendered_text") or "")[
                :_ACTIVE_LIFECYCLE_TEXT_LIMIT
            ]
            lifecycle.append(event_row)
        row["recent_lifecycle"] = lifecycle
        compact.append(row)
    return compact


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
        prefiltered = deterministic_ai_cost_prefilter(
            raw_text=raw_text,
            source_name=source_name,
            reply_context=reply_context,
            is_edit=is_edit,
            has_active_trade_context=bool(active_trade_context),
        )
        if prefiltered is not None:
            return prefiltered

        budget_reason = _paid_ai_budget_reason(source_status)
        if budget_reason is not None:
            return _budget_skip(raw_text, budget_reason)

        started = time.perf_counter()
        prompt = {
            "source_name": source_name,
            "source_status": source_status,
            "active_trade_context": _compact_active_context(active_trade_context),
            "recent_source_messages": _compact_recent_context(recent_source_messages),
            "telegram_message": raw_text,
            "reply_context": reply_context,
            "is_edit": is_edit,
            "previous_text": previous_text,
        }
        payload = {
            "model": self._model,
            "store": False,
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": _MAX_OUTPUT_TOKENS,
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
