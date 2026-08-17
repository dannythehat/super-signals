"""Fail-closed Super Signals message policy.

The OpenAI supervisor may understand provider grammar using bounded same-source context,
but this module is the final mechanical contract for what may progress to execution.
Only values literally present in the current Telegram message are allowed to satisfy a
new-trade execution gate. Explicit broker pending orders and explicitly declared entry
layers are supported; ambiguous or implicit layering still fails closed.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ai_message_supervisor import AiMessageDecision
from app.critical_entry_policy import (
    augment_management_actions,
    envelope,
    parse_critical_entries,
)
from app.day27_management_policy import extract_day27_management_actions

_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")
_INSTRUMENT = re.compile(r"\b(?:XAUUSD|GOLD)\b", re.IGNORECASE)
_BUY = re.compile(r"\bBUY(?:S|ING)?\b", re.IGNORECASE)
_SELL = re.compile(r"\bSELL(?:S|ING)?\b", re.IGNORECASE)
_PENDING = re.compile(r"\b(?:BUY|SELL)\s+(?:LIMITS?|STOPS?)\b|\bPENDING\b", re.IGNORECASE)
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
# Some providers intentionally post a bare immediate activation and then edit the same
# Telegram message into the complete structured signal. The edit may create the first
# canonical Signal only when the previous revision was already an unmistakable trade
# activation with the same side, instrument and first entry. This prevents arbitrary
# chatter/preparation from being "resurrected" into a broker order by a later edit.
_STRUCTURED_EDIT_COMPLETION = re.compile(
    r"(?is)\bENTRY\s*[:=@-]?\s*\d+(?:\.\d+)?\b"
    r".*\bSL\s*[:=@-]?\s*\d+(?:\.\d+)?\b"
    r".*\bTP\s*\d*\s*[:=@-]?\s*\d+(?:\.\d+)?\b"
)
_EDIT_FIRST_ENTRY = re.compile(
    r"(?im)^\s*(?:FIRST\s+)?ENTRY(?:\s*1)?\s*[:=@-]?\s*(\d+(?:\.\d+)?)\b"
)
_ACTIVATION_STUB = re.compile(
    r"(?is)^\s*(?:🔴|🟢|🔥|⚡|✅|🚨|\s)*"
    r"(BUY|SELL)\s+(?:XAUUSD|GOLD)\b"
    r"(?:\s+(?:NOW|AT))?\s*(?:@|:|=)?\s*(\d+(?:\.\d+)?)\s*[.!🔥✅\s]*$"
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


def _matching_activation_stub(
    previous_text: str | None,
    current_text: str,
    *,
    side: str,
) -> bool:
    if not previous_text:
        return False
    stub = _ACTIVATION_STUB.fullmatch(previous_text.strip())
    current_entry_match = _EDIT_FIRST_ENTRY.search(current_text)
    if stub is None or current_entry_match is None:
        return False
    if stub.group(1).upper() != side.strip().upper():
        return False
    old_price = _decimal(stub.group(2))
    new_price = _decimal(current_entry_match.group(1))
    return old_price is not None and new_price is not None and old_price == new_price


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
    entry_low = _decimal(extracted.get("entry_low"))
    entry_high = _decimal(extracted.get("entry_high"))
    stop_loss = _decimal(extracted.get("stop_loss"))
    take_profits = tuple(
        parsed
        for parsed in (_decimal(value) for value in (extracted.get("take_profits") or []))
        if parsed is not None
    )
    extracted["tp_open"] = bool(_OPEN_TARGET.search(raw_text))
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
        return (
            stop_loss < entry_low
            and all(target > entry_high for target in take_profits)
            and all(right > left for left, right in zip(take_profits, take_profits[1:]))
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
    previous_text: str | None = None,
) -> AiMessageDecision:
    """Return the mechanically allowed decision.

    Context can help the AI classify semantics, but cannot donate trade numbers. The
    current message alone must contain instrument, side, entry structure, SL and at
    least one numeric TP. Pending orders require an explicit LIMIT/STOP family. Entry
    layering requires explicit prices in a mechanically proven provider structure.
    """
    text = raw_text or ""

    if decision.decision == "new_trade":
        extracted, entry_low, entry_high, stop_loss, take_profits = _normalise_trade_values(
            decision, text
        )
        side = str(extracted.get("side") or "").strip().upper()

        edit_completed_first_trade = is_edit and not original_has_signal
        if edit_completed_first_trade:
            if _STRUCTURED_EDIT_COMPLETION.search(text) is None or not _matching_activation_stub(
                previous_text,
                text,
                side=side,
            ):
                return _skip(decision, "edit_cannot_create_first_trade", extracted)

        if _INSTRUMENT.search(text) is None:
            return _skip(decision, "missing_instrument", extracted)

        has_buy = _BUY.search(text) is not None
        has_sell = _SELL.search(text) is not None
        if side == "BUY" and not has_buy:
            return _skip(decision, "missing_side", extracted)
        if side == "SELL" and not has_sell:
            return _skip(decision, "missing_side", extracted)
        if side not in {"BUY", "SELL"} or has_buy == has_sell:
            return _skip(decision, "missing_side", extracted)

        try:
            critical_entries = parse_critical_entries(
                text,
                side=side,
                entry_low=entry_low,
                entry_high=entry_high,
            )
        except ValueError as exc:
            return _skip(decision, str(exc), extracted)

        if critical_entries:
            plan_low, plan_high = envelope(critical_entries)
            entry_low = plan_low
            entry_high = plan_high
            extracted["entry_plan"] = [
                {
                    "entry_index": item.entry_index,
                    "order_type": item.order_type,
                    "price": str(item.price),
                }
                for item in critical_entries
            ]
            all_pending = all(item.order_type != "market" for item in critical_entries)
            extracted["order_type"] = "pending" if all_pending else "market"
        elif _PENDING.search(text) or str(extracted.get("order_type") or "").lower() == "pending":
            return _skip(decision, "pending_order_type_ambiguous", extracted)

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
        # Intermediate TDC grid prices are mechanically derived from the two literal
        # zone endpoints. They are allowed only because parse_critical_entries proved
        # the exact HIGH RISK template; they are not required to appear as extra text.
        plan = extracted.get("entry_plan") or []
        literal_plan_prices = [
            _decimal(item.get("price"))
            for item in plan
            if isinstance(item, dict)
        ]
        if len(plan) <= 2:
            for parsed in literal_plan_prices:
                if parsed is not None:
                    required.add(parsed.normalize())
        if not required.issubset(literals):
            return _skip(decision, "literal_value_verification_failed", extracted)

        if not _directionally_valid(side, entry_low, entry_high, stop_loss, take_profits):
            return _skip(decision, "strict_directional_validation_failed", extracted)

        extracted.update(
            {
                "symbol": "XAUUSD",
                "side": side,
                "entry_low": str(entry_low),
                "entry_high": str(entry_high),
                "stop_loss": str(stop_loss),
                "take_profits": [str(value) for value in take_profits],
            }
        )

        has_pending = any(
            isinstance(item, dict) and item.get("order_type") != "market" for item in plan
        )
        if len(plan) > 1:
            reason = "v1_complete_layered_signal"
        elif has_pending:
            reason = "v1_complete_pending_signal"
        elif entry_low != entry_high:
            reason = "v1_complete_zone_signal"
        else:
            reason = "v1_complete_exact_signal"
        if edit_completed_first_trade:
            reason = f"{reason}_from_structured_edit"

        return replace(
            decision,
            decision="new_trade",
            action="execute",
            reason=reason,
            extracted=extracted,
        )

    if decision.decision == "trade_update":
        policy = extract_day27_management_actions(text)
        actions = augment_management_actions(text, policy.actions)
        if actions:
            extracted = dict(decision.extracted)
            normalized_actions = [dict(action) for action in actions]
            extracted["management_actions"] = normalized_actions
            first = normalized_actions[0]
            extracted["update_type"] = first.get("type")
            extracted["update_target"] = first.get("target")
            extracted["update_value"] = first.get("value")
            critical_targets = ("layer", "entry_", "best_entry", "all_but_best")
            return replace(
                decision,
                decision="trade_update",
                action="apply_update",
                reason=(
                    "layer_management_instruction"
                    if any(
                        any(token in str(action.get("target") or "") for token in critical_targets)
                        for action in normalized_actions
                    )
                    else policy.reason
                ),
                extracted=extracted,
            )

        if policy.reason in {"optional_management_instruction", "provider_result_only"}:
            return _ignore_update(decision, policy.reason)

        if _RESULT_ONLY.search(text):
            return _ignore_update(decision, "provider_result_only")
        return _ignore_update(decision)

    if decision.decision in {"chatter", "preparation"}:
        return replace(decision, action="ignore")
    return replace(decision, action="skip")


__all__ = ["apply_v1_message_policy"]
