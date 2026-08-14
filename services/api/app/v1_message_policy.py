"""Fail-closed Super Signals V1 message policy.

The OpenAI supervisor may understand provider grammar using bounded same-source context,
but this module is the final mechanical contract for what may progress to execution.
Only values literally present in the current Telegram message are allowed to satisfy a
new-trade execution gate. Unsupported or ambiguous content is skipped safely.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ai_message_supervisor import AiMessageDecision
from app.day27_management_policy import extract_day27_management_actions

_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")
_INSTRUMENT = re.compile(r"\b(?:XAUUSD|GOLD)\b", re.IGNORECASE)
_BUY = re.compile(r"\bBUY(?:S|ING)?\b", re.IGNORECASE)
_SELL = re.compile(r"\bSELL(?:S|ING)?\b", re.IGNORECASE)
_PENDING = re.compile(r"\b(?:BUY|SELL)\s+(?:LIMITS?|STOPS?)\b|\bPENDING\b", re.IGNORECASE)
_SECOND_ENTRY = re.compile(r"\b(?:SECOND|2ND)\s+ENTRY\b", re.IGNORECASE)
_FIRST_ENTRY = re.compile(
    r"(?im)^\s*(?:FIRST\s+)?ENTRY\s*[:=@-]?\s*(\d+(?:\.\d+)?)\b"
)
_OPEN_TARGET = re.compile(
    r"\b(?:TP\s*\d*\s*[:=@-]?\s*OPEN|TP\s+OPEN|RUNNER|LEAVE\s+(?:IT\s+)?OPEN)\b",
    re.IGNORECASE,
)
_DOUBLE_SIZE = re.compile(
    r"\b(?:DOUBLE|2X)\s+(?:LOTS?|LOT\s+SIZE|LOTSIZE|SIZE)\b",
    re.IGNORECASE,
)
_RESULT_ONLY = re.compile(
    r"\b(?:OUT\s+AT\s+BE|STOPPED\s+OUT|SL\s+HIT|TP\s*\d*\s+HIT|TP\s*\d*\s+INCOMING)\b"
    r"|(?:^|\s)[+-]\s*\d+(?:\.\d+)?\s*PIPS?\b",
    re.IGNORECASE,
)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
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


def _skip(
    decision: AiMessageDecision,
    reason: str,
    extracted: dict[str, Any] | None = None,
) -> AiMessageDecision:
    return replace(
        decision,
        action="skip",
        reason=reason,
        extracted=dict(decision.extracted if extracted is None else extracted),
    )


def _ignore_update(
    decision: AiMessageDecision,
    reason: str = "unsupported_management",
) -> AiMessageDecision:
    extracted = dict(decision.extracted)
    extracted["update_type"] = None
    extracted["update_target"] = None
    extracted["update_value"] = None
    extracted["management_actions"] = []
    return replace(
        decision,
        decision="trade_update",
        action="ignore",
        reason=reason,
        extracted=extracted,
    )


def _normalise_trade_values(
    decision: AiMessageDecision,
    raw_text: str,
) -> tuple[
    dict[str, Any],
    Decimal | None,
    Decimal | None,
    Decimal | None,
    tuple[Decimal, ...],
]:
    extracted = dict(decision.extracted)

    # TIG V1: when the provider explicitly labels a second entry, use only ENTRY 1.
    # We do not infer a layer, range, or risk split.
    if _SECOND_ENTRY.search(raw_text):
        first = _FIRST_ENTRY.search(raw_text)
        if first is None:
            return extracted, None, None, None, ()
        extracted["entry_low"] = first.group(1)
        extracted["entry_high"] = first.group(1)

    entry_low = _decimal(extracted.get("entry_low"))
    entry_high = _decimal(extracted.get("entry_high"))
    stop_loss = _decimal(extracted.get("stop_loss"))
    take_profits = tuple(
        parsed
        for parsed in (_decimal(value) for value in (extracted.get("take_profits") or []))
        if parsed is not None
    )
    extracted["tp_open"] = bool(_OPEN_TARGET.search(raw_text))
    # Provider sizing is mechanical: only an explicit literal in the current message
    # can enable double size. Do not depend on the AI model echoing the boolean.
    extracted["double_lot"] = bool(_DOUBLE_SIZE.search(raw_text))
    return extracted, entry_low, entry_high, stop_loss, take_profits


def _directionally_valid(
    side: str,
    entry_low: Decimal,
    entry_high: Decimal,
    stop_loss: Decimal,
    take_profits: tuple[Decimal, ...],
) -> bool:
    if entry_high < entry_low:
        return False
    if side == "BUY":
        # A market-zone fill can happen anywhere in the provider's zone. Requiring
        # SL below the low edge and TPs above the high edge keeps every allowed fill valid.
        return (
            stop_loss < entry_low
            and all(target > entry_high for target in take_profits)
            and all(
                right > left
                for left, right in zip(take_profits, take_profits[1:])
            )
        )
    return (
        stop_loss > entry_high
        and all(target < entry_low for target in take_profits)
        and all(right < left for left, right in zip(take_profits, take_profits[1:]))
    )


def apply_v1_message_policy(
    decision: AiMessageDecision,
    *,
    raw_text: str,
    is_edit: bool = False,
    original_has_signal: bool = False,
) -> AiMessageDecision:
    """Return the mechanically allowed V1 decision.

    Context can help the AI classify semantics, but cannot make a trade executable.
    The current message alone must contain instrument, side, entry, SL and >=1 numeric TP.
    Day 27 management is similarly current-message-only and fail-closed.

    An edit can revalidate a signal that already exists, but it can never author the
    first one. ``original_has_signal`` states whether the edited provider message had
    already produced a canonical Signal, and defaults to False so any caller that
    cannot answer that question fails closed.
    """
    text = raw_text or ""

    if decision.decision == "new_trade":
        # Working Blueprint, locked: "An initially skipped setup is not resurrected
        # into a new trade by a later edit." A preparation, incomplete or invalid
        # original produced no Signal, so an edit that now looks complete must not
        # become the first executable trade. This is enforced mechanically rather
        # than left to the supervisor happening to classify the edit conservatively.
        # Edits to a message that already owns a Signal still revalidate normally,
        # and Day 18's unique (provider_chat_id, provider_message_id) constraint
        # keeps that path from ever creating a second Signal.
        if is_edit and not original_has_signal:
            return _skip(decision, "edit_cannot_create_first_trade")

        extracted, entry_low, entry_high, stop_loss, take_profits = _normalise_trade_values(
            decision, text
        )

        if _PENDING.search(text) or str(extracted.get("order_type") or "").lower() == "pending":
            return _skip(decision, "unsupported_pending_order", extracted)

        if _INSTRUMENT.search(text) is None:
            return _skip(decision, "missing_instrument", extracted)

        has_buy = _BUY.search(text) is not None
        has_sell = _SELL.search(text) is not None
        side = str(extracted.get("side") or "").strip().upper()
        if side == "BUY" and not has_buy:
            return _skip(decision, "missing_side", extracted)
        if side == "SELL" and not has_sell:
            return _skip(decision, "missing_side", extracted)
        if side not in {"BUY", "SELL"} or has_buy == has_sell:
            return _skip(decision, "missing_side", extracted)

        if entry_low is None or entry_high is None:
            return _skip(decision, "missing_entry", extracted)
        if stop_loss is None:
            return _skip(decision, "missing_sl", extracted)
        if not take_profits:
            return _skip(decision, "missing_tp", extracted)

        literals = _literal_numbers(text)
        required = {
            entry_low.normalize(),
            entry_high.normalize(),
            stop_loss.normalize(),
            *(value.normalize() for value in take_profits),
        }
        if not required.issubset(literals):
            return _skip(decision, "literal_value_verification_failed", extracted)

        if not _directionally_valid(side, entry_low, entry_high, stop_loss, take_profits):
            return _skip(decision, "strict_directional_validation_failed", extracted)

        extracted.update(
            {
                "symbol": "XAUUSD",
                "side": side,
                "order_type": "market",
                "entry_low": str(entry_low),
                "entry_high": str(entry_high),
                "stop_loss": str(stop_loss),
                "take_profits": [str(value) for value in take_profits],
            }
        )
        return replace(
            decision,
            decision="new_trade",
            action="execute",
            reason=(
                "v1_complete_zone_signal"
                if entry_low != entry_high
                else "v1_complete_exact_signal"
            ),
            extracted=extracted,
        )

    if decision.decision == "trade_update":
        policy = extract_day27_management_actions(text)
        if policy.actions:
            extracted = dict(decision.extracted)
            actions = [dict(action) for action in policy.actions]
            extracted["management_actions"] = actions
            first = actions[0]
            extracted["update_type"] = first.get("type")
            extracted["update_target"] = first.get("target")
            extracted["update_value"] = first.get("value")
            return replace(
                decision,
                decision="trade_update",
                action="apply_update",
                reason=policy.reason,
                extracted=extracted,
            )

        if policy.reason in {"optional_management_instruction", "provider_result_only"}:
            return _ignore_update(decision, policy.reason)

        # Provider result statements never create a new management action. This check
        # deliberately runs after the Day 27 extractor so a combined explicit command
        # such as "TP1 hit, move SL to 4385" may still produce the explicit SL action.
        if _RESULT_ONLY.search(text):
            return _ignore_update(decision, "provider_result_only")
        return _ignore_update(decision)

    # Chatter, preparation and genuinely unclear messages remain non-executable.
    if decision.decision in {"chatter", "preparation"}:
        return replace(decision, action="ignore")
    return replace(decision, action="skip")


__all__ = ["apply_v1_message_policy"]
