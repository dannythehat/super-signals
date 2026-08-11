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

SUPER SIGNALS V1 EXECUTION BOUNDARY
Use action=execute ONLY when the provider message itself contains all of the following:
1. XAUUSD/GOLD side BUY or SELL
2. one single exact market entry price
3. one explicit numeric stop loss
4. one or more explicit numeric take-profit prices
5. no second/discrete entry, no entry range, no pending/limit order, and no open-ended target

If a message is a genuine trade instruction but its structure is outside that exact
V1 boundary, understand and extract what is explicit, but use decision=new_trade and
action=skip. Use one of these stable reasons when applicable:
- unsupported_multiple_entries: two or more separately stated entry prices, including
  wording such as 'ENTRY ... Second entry ...'
- unsupported_entry_range: a range/area/slash pair such as 4392-4388 or 4396/4391
- unsupported_pending_order: BUY LIMIT, SELL LIMIT, BUY STOP, SELL STOP or equivalent
- unsupported_open_target: TP OPEN, RUNNER, leave open, or another non-numeric target

When several unsupported structures occur together, prefer the most mechanically
fundamental reason in this order: unsupported_multiple_entries,
unsupported_pending_order, unsupported_entry_range, unsupported_open_target.

Examples of important real-world distinctions:
- 'Ready', 'Prepare for a buy', 'Watch gold', 'Get ready' => preparation + ignore.
- 'Buy Gold Now' or 'Sell Gold Now' with no explicit entry, SL and numeric TP values
  in that message/context => non_actionable + skip, reason=provider_instruction_incomplete.
- 'BUY GOLD @ 4371 / TP 4375 / TP 4380 / SL 4360' => exact market trade and may execute.
- 'BUY GOLD @ 4396/4391 ...' => new_trade + skip, unsupported_entry_range.
- 'ENTRY: 4385 / Second entry: 4380 ...' => new_trade + skip,
  unsupported_multiple_entries.
- 'BUY LIMITS GOLD ...' => new_trade + skip, unsupported_pending_order.
- A message containing otherwise numeric TPs plus 'TP OPEN' => new_trade + skip,
  unsupported_open_target. Preserve only explicit numeric targets in take_profits.

Treat GOLD as XAUUSD when the provider is clearly referring to gold. For an explicit
numeric range, set entry_low to the lower number and entry_high to the higher number.
If only one exact entry is supplied, set both to that same value. BUY LIMIT/SELL LIMIT
are pending orders. 'High risk', 'risk free', 'secure profit', large pip claims, emojis,
or emphatic wording do NOT mean double lot. Only explicit 'double lot', 'double lots',
'double size' or unmistakably equivalent sizing wording sets double_lot=true.

For trade updates, use action=apply_update only for an explicit management or result
instruction that can be identified from the message/context. A bare profit boast such
as '+100 pips' with no clear linked action may be a result_report or non_actionable;
do not invent which trade or TP it belongs to. TP1/TP2/etc HIT is a trade_update with
update_type=tp_hit. Explicit 'move SL to 4385' is edit_stop_loss. Explicit 'close half'
or 'take partials' is close_half. Explicit 'close/out at entry/on the rest' is close.

When is_edit=true, the Telegram message is a revision of the same provider message,
not a second trade. Compare previous_text with telegram_message. If the edit changes
an existing signal instruction, classify it as trade_update/action=apply_update and
return the full explicit revised trade fields plus the most specific update_type you
can identify. If there is no actionable instruction change, ignore or skip it. Never
turn an edit into a duplicate new trade.

Use action=ignore for chatter or preparation. Use action=skip for incomplete,
unsupported or unclear instructions. Return only the requested structured object."""


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
        timeout_seconds: int = 12,
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
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": 1200,
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
