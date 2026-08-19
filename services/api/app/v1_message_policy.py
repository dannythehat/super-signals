"""Canonical fail-closed Super Signals message policy.

OpenAI may understand provider grammar using bounded same-source context, but this
module is the final mechanical contract for execution. Trade numbers must come from
the current Telegram message. A tightly-scoped source profile may supply only a known
instrument identity for a dedicated Gold/XAUUSD provider; it can never donate entry,
SL, TP, order type or size.

The only exception to provider-supplied SL/TP is the exact standalone Gold/XAUUSD NOW
product profile. That whole-message command carries no invented provider prices in the
canonical Signal; broker protection is derived later by the execution engine.
"""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any

from app.ai_message_supervisor import AiMessageDecision
from app.bare_gold_now_policy import PROFILE as BARE_NOW_PROFILE, bare_now_side
from app.critical_entry_policy import augment_management_actions, envelope, parse_critical_entries
from app.day27_management_policy import extract_day27_management_actions

_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:\.\d+)?(?![A-Za-z0-9_.])")
_INSTRUMENT = re.compile(r"\b(?:XAUUSD|GOLD)\b", re.IGNORECASE)
_BUY = re.compile(r"\bBUY(?:S|ING)?\b", re.IGNORECASE)
# Observed TGC spellings are mechanical side evidence only. They do not donate an
# instrument or any price, SL, TP, size or order type.
_SELL = re.compile(r"\b(?:SELL(?:S|ING)?|SELING|SELLIMG)\b", re.IGNORECASE)
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

_XAUUSD_SOURCE_PROFILES = {"tgc_xauusd", "tdc_xauusd"}

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


def _profile_supplies_xauusd(source_profile: str | None) -> bool:
    return str(source_profile or "").strip().lower() in _XAUUSD_SOURCE_PROFILES


def _same_trade_progressive_edit(
    previous_text: str | None,
    *,
    side: str,
    entry_low: Decimal | None,
    entry_high: Decimal | None,
    source_profile: str | None,
) -> bool:
    if not previous_text or side not in {"BUY", "SELL"}:
        return False
    previous = previous_text.strip()
    stub = _ACTIVATION_STUB.fullmatch(previous)
    if stub is not None:
        if stub.group(1).upper() != side:
            return False
        stub_price = _decimal(stub.group(2))
        current_entries = {value for value in (entry_low, entry_high) if value is not None}
        return stub_price is not None and stub_price in current_entries
    if _INSTRUMENT.search(previous) is None and not _profile_supplies_xauusd(source_profile):
        return False
    previous_has_buy = _BUY.search(previous) is not None
    previous_has_sell = _SELL.search(previous) is not None
    if side == "BUY" and (not previous_has_buy or previous_has_sell):
        return False
    if side == "SELL" and (not previous_has_sell or previous_has_buy):
        return False
    current_entries = {
        value.normalize() for value in (entry_low, entry_high) if value is not None
    }
    if not current_entries:
        return False
    return bool(_literal_numbers(previous) & current_entries)


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
    if entry_low is not None and entry_high is not None and entry_low > entry_high:
        entry_low, entry_high = entry_high, entry_low
    stop_loss = _decimal(extracted.get("stop_loss"))
    take_profits = tuple(
        parsed
        for parsed in (_decimal(value) for value in (extracted.get("take_profits") or []))
        if parsed is not None
    )
    extracted["tp_open"] = bool(_OPEN_TARGET.search(raw_text))
    extracted["double_lot"] = bool(_DOUBLE_SIZE.search(raw_text))
    return extracted, entry_low, entry_high, stop_loss, take_profits


def _ordered_targets(side: str, targets: tuple[Decimal, ...]) -> bool:
    if not targets:
        return False
    if side == "BUY":
        return all(right > left for left, right in zip(targets, targets[1:]))
    if side == "SELL":
        return all(right < left for left, right in zip(targets, targets[1:]))
    return False


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
            and _ordered_targets(side, take_profits)
        )
    return (
        stop_loss > entry_high
        and all(target < entry_low for target in take_profits)
        and _ordered_targets(side, take_profits)
    )


def apply_v1_message_policy(
    decision: AiMessageDecision,
    *,
    raw_text: str,
    is_edit: bool = False,
    original_has_signal: bool | None = None,
    previous_text: str | None = None,
) -> AiMessageDecision:
    """Return the mechanically allowed decision."""
    text = raw_text or ""

    if decision.decision == "new_trade":
        exact_bare_side = bare_now_side(text)
        if exact_bare_side is not None:
            if is_edit:
                return _skip(decision, "bare_gold_now_edit_not_executable")
            extracted = dict(decision.extracted or {})
            extracted.update(
                {
                    "symbol": "XAUUSD",
                    "side": exact_bare_side,
                    "order_type": "market",
                    "entry_low": None,
                    "entry_high": None,
                    "stop_loss": None,
                    "take_profits": [],
                    "double_lot": False,
                    "tp_open": False,
                    "execution_profile": BARE_NOW_PROFILE,
                }
            )
            return replace(
                decision,
                decision="new_trade",
                action="execute",
                reason=BARE_NOW_PROFILE,
                extracted=extracted,
            )

        extracted, entry_low, entry_high, stop_loss, take_profits = _normalise_trade_values(
            decision, text
        )
        side = str(extracted.get("side") or "").strip().upper()
        source_profile = str(extracted.get("source_profile") or "").strip().lower() or None

        if is_edit and original_has_signal is None:
            return _skip(decision, "edit_cannot_create_first_trade", extracted)
        edit_completed_first_trade = is_edit and original_has_signal is False
        if edit_completed_first_trade:
            if previous_text is None or previous_text.strip() == text.strip():
                return _skip(decision, "edit_cannot_create_first_trade", extracted)
            if not _same_trade_progressive_edit(
                previous_text,
                side=side,
                entry_low=entry_low,
                entry_high=entry_high,
                source_profile=source_profile,
            ):
                return _skip(decision, "edit_cannot_create_first_trade", extracted)

        if _INSTRUMENT.search(text) is None and not _profile_supplies_xauusd(source_profile):
            return _skip(decision, "missing_instrument", extracted)

        has_buy = _BUY.search(text) is not None
        has_sell = _SELL.search(text) is not None
        if side == "BUY" and not has_buy:
            return _skip(decision, "missing_side", extracted)
        if side == "SELL" and not has_sell:
            return _skip(decision, "missing_side", extracted)
        if side not in {"BUY", "SELL"} or has_buy == has_sell:
            return _skip(decision, "missing_side", extracted)

        no_entry_market = (
            entry_low is None
            and entry_high is None
            and _PENDING.search(text) is None
        )
        if (entry_low is None) != (entry_high is None):
            return _skip(decision, "signal_entry_invalid", extracted)

        if no_entry_market:
            critical_entries = ()
        else:
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
            if entry_low is None or entry_high is None:
                entry_low, entry_high = plan_low, plan_high
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
        elif _PENDING.search(text):
            return _skip(decision, "pending_order_type_ambiguous", extracted)
        elif str(extracted.get("order_type") or "").strip().lower() == "pending":
            extracted["order_type"] = "market"

        if stop_loss is None:
            return _skip(decision, "missing_sl", extracted)
        if not take_profits:
            return _skip(decision, "missing_tp", extracted)

        literals = _literal_numbers(text)
        if no_entry_market:
            required = {
                stop_loss.normalize(),
                *(value.normalize() for value in take_profits),
            }
            if not required.issubset(literals):
                return _skip(decision, "literal_value_verification_failed", extracted)
            if not _ordered_targets(side, take_profits):
                return _skip(decision, "strict_directional_validation_failed", extracted)
            extracted.update(
                {
                    "symbol": "XAUUSD",
                    "side": side,
                    "order_type": "market",
                    "entry_low": None,
                    "entry_high": None,
                    "stop_loss": str(stop_loss),
                    "take_profits": [str(value) for value in take_profits],
                }
            )
            return replace(
                decision,
                decision="new_trade",
                action="execute",
                reason="v1_complete_market_signal_live_entry",
                extracted=extracted,
            )

        if entry_low is None or entry_high is None:
            return _skip(decision, "missing_entry", extracted)

        required = {
            entry_low.normalize(),
            entry_high.normalize(),
            stop_loss.normalize(),
            *(value.normalize() for value in take_profits),
        }
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
