"""Immediate AI interpretation for Telegram source messages and edits.

The supervisor answers only what the provider explicitly instructed. It never invents
entry, stop loss, take profit, sizing, or an exit. Every call returns a strict
machine-readable decision so the live path never waits for human review.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

import httpx

from app.provider_language_profiles import provider_profile


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
Your only job is to determine what the signal provider explicitly means and instructs.
Never make a trading recommendation. Never decide whether a setup is good or bad.
Never invent, improve, adjust, or substitute a numeric entry, stop loss, take profit,
lot size, or exit that the provider did not explicitly state in the supplied evidence.

SOURCE-AWARE INTERPRETATION
You receive source_name plus a bounded recent_source_messages history from that SAME
Telegram source. Use that history to learn how this provider communicates: how they
prepare, open trades, layer entries, report targets, cancel, edit, close, celebrate,
and chat. Different providers use different grammar. Do not assume every provider
posts a complete trade in one message and do not treat a genuine provider trade trigger
as chatter merely because its SL/TP are elsewhere or not yet supplied.

recent_source_messages is context for SEMANTIC UNDERSTANDING and lifecycle linking. It
is not permission to invent or silently carry forward old numeric trade parameters.
For action=execute, numeric execution evidence may come only from telegram_message and
a directly linked reply_context (or the current edited message itself). Do not pull a
price, SL or TP from unrelated ambient history just because it looks plausible.

DECISION AND ACTION ARE DIFFERENT QUESTIONS
First decide what the current provider message IS. Then decide what the system may DO.
- new_trade: provider is actually opening/placing/activating a new trade. This remains
  new_trade even when the instruction is incomplete or unsupported; use action=skip.
- trade_update: provider is managing, reporting or closing an existing trade.
- chatter: conversation, celebration unrelated to a specific trade, marketing, social
  prompts, general commentary or greetings.
- preparation: heads-up/watch/wait/get-ready language before an actual entry trigger.
- non_actionable: truly unclear content where the evidence does not establish a trade,
  update, preparation or ordinary chatter.

Examples of semantic intent:
- In a provider whose recent pattern repeatedly uses 'I'm buying 4399' / 'I'm selling
  4391' as actual entries followed by TP result posts, a fresh 'I'm buying 4375' is a
  new_trade, not chatter. If no explicit SL/TP evidence is directly linked, action=skip
  with reason=provider_instruction_incomplete.
- 'Buy Gold Now' or 'Sell Gold Now' is an entry activation, not preparation. If required
  execution parameters are absent, classify new_trade + skip; do not call it chatter
  or preparation.
- 'WHO IS READYY?', 'Get Ready', 'Prepare for a buy' are preparation/chatter unless the
  current message itself actually activates an entry.
- Promotional posts, giveaways, requests for comments/screenshots and general market
  discussion are chatter even if nearby messages contain a trade.
- 'TP1 HIT', 'SL HIT', 'close 3 layers', 'set breakeven', 'out at entry' are trade_update
  when context/reply evidence identifies them as lifecycle messages.

CURRENT EXECUTION CAPABILITIES
A complete literal XAUUSD/GOLD setup may be executed when it has an explicit side,
entry evidence, numeric stop loss and at least one numeric take profit. Supported
structures include exact market entries, entry zones, explicit second/layered entries,
literal BUY/SELL LIMIT or STOP orders, and numeric targets plus an open runner.
Extract every literal boundary and numeric target. A complete structured edit is still
the same Telegram instruction and must be classified new_trade when no trade was
previously created; canonical idempotency prevents duplicate execution.

Use new_trade + skip only when required current-message evidence is genuinely missing,
contradictory or unsafe. Do not label a complete entry structure trade_update merely
because is_edit=true. The deterministic policy independently verifies every number,
direction, order structure and duplicate before execution.

Treat GOLD as XAUUSD when the provider/context clearly refers to gold. For an explicit
numeric range, set entry_low to the lower number and entry_high to the higher number.
If only one exact entry is supplied, set both to that same value. BUY LIMIT/SELL LIMIT
are pending orders. 'High risk', 'risk free', 'secure profit', large pip claims, emojis,
or emphatic wording do NOT mean double lot. Only explicit 'double lot', 'double lots',
'double size' or unmistakably equivalent sizing wording sets double_lot=true.

For trade updates, use action=apply_update for an explicit management/result event
that can be linked from reply_context or the same source's recent sequence without
inventing a target or trade. TP1/TP2/etc HIT is update_type=tp_hit. SL HIT is a
result_report. Explicit 'move SL to 4385' is edit_stop_loss. Explicit 'close half' or
'close 3 layers' is close_half. Explicit 'close/out at entry/on the rest' is close.
A bare '+100 pips' may be a result_report when the provider sequence clearly links it;
otherwise do not invent which trade or TP it belongs to.

When is_edit=true, the Telegram message is a revision of the same provider message,
not a second Telegram post. Compare previous_text with telegram_message. If an
incomplete trigger was edited into a complete setup, classify the current revision as
new_trade and return all literal fields; canonical message idempotency activates it
once. If an already-created/executed signal is being changed, classify an explicit
management change as trade_update. If there is no actionable change, ignore or skip.

Use action=ignore for chatter or preparation. Use action=skip for incomplete,
unsupported or truly unclear instructions. Return only the requested structured object."""

_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")
_OPEN_TARGET = re.compile(
    r"\b(?:TP\s*\d*\s*[:=-]?\s*OPEN|TP\s+OPEN|RUNNER|LEAVE\s+(?:IT\s+)?OPEN)\b",
    re.IGNORECASE,
)
_SECOND_ENTRY = re.compile(r"\b(?:SECOND|2ND)\s+ENTRY\b", re.IGNORECASE)
_DOUBLE_SIZE = re.compile(
    r"\b(?:DOUBLE\s+(?:LOT|LOTS|SIZE)|2X\s+(?:LOT|LOTS|SIZE))\b",
    re.IGNORECASE,
)
_ACTIVE_GOLD_NOW = re.compile(
    r"^\s*(?:(BUY|SELL)\s+(?:GOLD|XAUUSD)\s+NOW|(?:GOLD|XAUUSD)\s+(BUY|SELL)\s+NOW)\s*[!✅🔥🚀]*\s*$",
    re.IGNORECASE,
)
_TERSE_PRICE_ENTRY = re.compile(
    r"^\s*I[’']?M\s+(BUYING|SELLING)\s+(\d+(?:\.\d+)?)\s*[!✅🔥🚀]*\s*$",
    re.IGNORECASE,
)


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


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _literal_numbers(raw_text: str) -> set[Decimal]:
    values: set[Decimal] = set()
    for token in _NUMBER_TOKEN.findall(raw_text):
        parsed = _decimal(token)
        if parsed is not None:
            values.add(parsed.normalize())
    return values


def _guard_provider_intent(
    parsed: dict[str, Any],
    raw_text: str,
    *,
    is_edit: bool,
) -> dict[str, Any]:
    """Prevent explicit entry activations from being downgraded to chatter/preparation.

    This guard asserts semantic intent only. It does not make an incomplete trade
    executable and it does not import numeric values from ambient source history.
    """
    guarded = dict(parsed)
    if is_edit:
        return guarded

    active = _ACTIVE_GOLD_NOW.fullmatch(raw_text)
    terse = _TERSE_PRICE_ENTRY.fullmatch(raw_text)
    if active is None and terse is None:
        return guarded

    guarded["decision"] = "new_trade"
    if guarded.get("action") != "execute":
        guarded["action"] = "skip"
        guarded["reason"] = "provider_instruction_incomplete"

    if active is not None:
        side = (active.group(1) or active.group(2) or "").upper()
        guarded["side"] = side
        guarded["symbol"] = "XAUUSD"
        guarded["order_type"] = guarded.get("order_type") or "market"
        return guarded

    assert terse is not None
    guarded["side"] = "BUY" if terse.group(1).upper() == "BUYING" else "SELL"
    entry = terse.group(2)
    guarded["entry_low"] = entry
    guarded["entry_high"] = entry
    guarded["order_type"] = guarded.get("order_type") or "market"
    return guarded


def _guard_execute_decision(
    parsed: dict[str, Any],
    raw_text: str,
    *,
    linked_context: str | None = None,
) -> dict[str, Any]:
    """Mechanically enforce the V1 execution boundary after the model responds.

    AI may use recent same-source history to understand provider grammar, but ambient
    history can never satisfy execution evidence. Only the current provider message and
    its directly linked reply are accepted by this mechanical guard.
    """
    guarded = dict(parsed)
    evidence_text = raw_text
    if linked_context:
        evidence_text = f"{raw_text}\n\nDIRECT REPLY CONTEXT:\n{linked_context}"

    if bool(guarded.get("double_lot")) and not _DOUBLE_SIZE.search(evidence_text):
        guarded["double_lot"] = False

    if guarded.get("decision") != "new_trade" or guarded.get("action") != "execute":
        return guarded

    upper_text = evidence_text.upper()
    symbol = str(guarded.get("symbol") or "").strip().upper()
    if symbol == "GOLD":
        symbol = "XAUUSD"
        guarded["symbol"] = symbol
    side = str(guarded.get("side") or "").strip().upper()
    order_type = str(guarded.get("order_type") or "").strip().lower()

    if symbol != "XAUUSD" or not ("XAUUSD" in upper_text or "GOLD" in upper_text):
        guarded["action"] = "skip"
        guarded["reason"] = "provider_instruction_incomplete"
        return guarded
    if side not in {"BUY", "SELL"} or side not in upper_text:
        guarded["action"] = "skip"
        guarded["reason"] = "provider_instruction_incomplete"
        return guarded
    if order_type != "market":
        guarded["action"] = "skip"
        guarded["reason"] = "unsupported_pending_order"
        return guarded
    if _SECOND_ENTRY.search(evidence_text):
        guarded["action"] = "skip"
        guarded["reason"] = "unsupported_multiple_entries"
        return guarded
    if _OPEN_TARGET.search(evidence_text):
        guarded["action"] = "skip"
        guarded["reason"] = "unsupported_open_target"
        return guarded

    entry_low = _decimal(guarded.get("entry_low"))
    entry_high = _decimal(guarded.get("entry_high"))
    stop_loss = _decimal(guarded.get("stop_loss"))
    take_profits = tuple(_decimal(value) for value in (guarded.get("take_profits") or []))
    if (
        entry_low is None
        or entry_high is None
        or stop_loss is None
        or not take_profits
        or any(value is None for value in take_profits)
    ):
        guarded["action"] = "skip"
        guarded["reason"] = "provider_instruction_incomplete"
        return guarded
    if entry_low != entry_high:
        guarded["action"] = "skip"
        guarded["reason"] = "unsupported_entry_range"
        return guarded

    literals = _literal_numbers(evidence_text)
    required_literals = {
        entry_low.normalize(),
        entry_high.normalize(),
        stop_loss.normalize(),
        *(value.normalize() for value in take_profits if value is not None),
    }
    if not required_literals.issubset(literals):
        guarded["action"] = "skip"
        guarded["reason"] = "literal_value_verification_failed"
        return guarded

    concrete_targets = tuple(value for value in take_profits if value is not None)
    entry = entry_low
    if side == "BUY":
        valid_direction = (
            stop_loss < entry
            and all(target > entry for target in concrete_targets)
            and all(right > left for left, right in zip(concrete_targets, concrete_targets[1:]))
        )
    else:
        valid_direction = (
            stop_loss > entry
            and all(target < entry for target in concrete_targets)
            and all(right < left for left, right in zip(concrete_targets, concrete_targets[1:]))
        )
    if not valid_direction:
        guarded["action"] = "skip"
        guarded["reason"] = "strict_directional_validation_failed"
        return guarded

    return guarded


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
            "provider_language_profile": provider_profile(source_name),
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

    @staticmethod
    def _output_text(body: dict[str, Any]) -> str:
        for item in body.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return str(content["text"])
        raise AiSupervisorError("ai_supervisor_output_missing")
