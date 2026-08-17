"""Deterministic pending/layer interpretation for critical trade infrastructure.

The AI supervisor may understand provider dialect, but broker structure is derived here
from literal current-message evidence only. No price, SL or TP is borrowed from ambient
history. Explicit multi-entry setups are represented before execution so risk can be
shared across the declared layers instead of being silently multiplied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


@dataclass(frozen=True, slots=True)
class CriticalEntry:
    entry_index: int
    order_type: str
    price: Decimal


# "BUY LIMITS ... 4332/4326 AREA" declares a layer *zone* but not the actual
# layer count or prices. Never silently convert that into one order at the first
# number or invent a grid. A future provider-specific rule can only promote it once
# the provider's exact layer convention is proven.
_AMBIGUOUS_PENDING_ZONE = re.compile(
    r"\b(?:BUY|SELL)\s+(?:LIMITS|STOPS)\b[^\n]{0,60}"
    r"\d+(?:\.\d+)?\s*(?:/|-|TO)\s*\d+(?:\.\d+)?",
    re.IGNORECASE,
)
_EXPLICIT_PENDING = re.compile(
    r"\b(BUY|SELL)\s+(LIMIT|STOP)S?\b(?:\s+(?:XAUUSD|GOLD))?\s*(?:@|AT|:|=)?\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_FIRST_ENTRY = re.compile(
    r"(?im)^\s*(?:FIRST\s+ENTRY|ENTRY(?:\s*1)?)\s*[:=@-]?\s*(\d+(?:\.\d+)?)\b"
)
_SECOND_ENTRY = re.compile(
    r"(?im)^\s*(?:SECOND\s+ENTRY|2ND\s+ENTRY|ENTRY\s*2)\s*[:=@-]?\s*(\d+(?:\.\d+)?)\b"
)
_NTH_ENTRY = re.compile(
    r"(?im)^\s*(?:(THIRD\s+ENTRY|3RD\s+ENTRY|ENTRY\s*3)|(FOURTH\s+ENTRY|4TH\s+ENTRY|ENTRY\s*4)|(FIFTH\s+ENTRY|5TH\s+ENTRY|ENTRY\s*5))\s*[:=@-]?\s*(\d+(?:\.\d+)?)\b"
)
_CLOSE_LAYERS = re.compile(
    r"\bCLOSE\s+(\d+)\s+LAYERS?\b",
    re.IGNORECASE,
)
_LEAVE_BEST = re.compile(
    r"\b(?:LEAVE|KEEP)\s+(?:THE\s+)?BEST(?:\s+(?:ENTRY|LAYER|ONE))?\s+(?:RUNNING|OPEN)\b",
    re.IGNORECASE,
)
_SECOND_ENTRY_CONTEXT = re.compile(r"\b(?:SECOND|2ND)\s+ENTRY\b|\bENTRY\s*2\b", re.IGNORECASE)
_FIRST_ENTRY_CONTEXT = re.compile(r"\bFIRST\s+ENTRY\b|\bENTRY\s*1\b", re.IGNORECASE)
_PARTIAL = re.compile(
    r"\b(?:BOOK|TAKE|CLOSE|BANK|SECURE)\b[^\n]{0,35}"
    r"\b(?:PARTIALS?|HALF|SOME\s+PROFIT|PROFIT\s+OFF)\b",
    re.IGNORECASE,
)


def _price(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _pending_type(side: str, word: str) -> str:
    return f"{side.lower()}_{word.lower()}"


def _derived_layer_type(side: str, *, first: Decimal, later: Decimal) -> str:
    """Derive the broker order family from the provider's declared entry sequence."""
    normalized = side.strip().upper()
    if normalized == "BUY":
        return "buy_limit" if later < first else "buy_stop"
    if normalized == "SELL":
        return "sell_limit" if later > first else "sell_stop"
    raise ValueError("trade_side_invalid")


def parse_critical_entries(
    raw_text: str,
    *,
    side: str,
    entry_low: object,
    entry_high: object,
) -> tuple[CriticalEntry, ...]:
    """Return exact pending/layer entries, or an empty tuple for an ordinary market zone."""
    text = raw_text or ""
    normalized_side = side.strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("trade_side_invalid")

    if _AMBIGUOUS_PENDING_ZONE.search(text) is not None:
        raise ValueError("pending_layer_grid_unspecified")

    pending = _EXPLICIT_PENDING.search(text)
    if pending is not None:
        pending_side = pending.group(1).upper()
        if pending_side != normalized_side:
            raise ValueError("pending_side_mismatch")
        price = _price(pending.group(3))
        if price is None:
            raise ValueError("pending_entry_invalid")
        return (
            CriticalEntry(
                entry_index=1,
                order_type=_pending_type(pending_side, pending.group(2)),
                price=price,
            ),
        )

    second_match = _SECOND_ENTRY.search(text)
    if second_match is not None:
        first_match = _FIRST_ENTRY.search(text)
        if first_match is None:
            raise ValueError("layer_first_entry_missing")
        first = _price(first_match.group(1))
        second = _price(second_match.group(1))
        if first is None or second is None or first == second:
            raise ValueError("layer_entry_invalid")
        entries: list[CriticalEntry] = [
            CriticalEntry(entry_index=1, order_type="market", price=first),
            CriticalEntry(
                entry_index=2,
                order_type=_derived_layer_type(normalized_side, first=first, later=second),
                price=second,
            ),
        ]
        for match in _NTH_ENTRY.finditer(text):
            index = 3 if match.group(1) else 4 if match.group(2) else 5
            later = _price(match.group(4))
            if later is None or any(item.price == later for item in entries):
                raise ValueError("layer_entry_invalid")
            entries.append(
                CriticalEntry(
                    entry_index=index,
                    order_type=_derived_layer_type(
                        normalized_side,
                        first=first,
                        later=later,
                    ),
                    price=later,
                )
            )
        return tuple(sorted(entries, key=lambda item: item.entry_index))

    low = _price(entry_low)
    high = _price(entry_high)
    if low is None or high is None:
        raise ValueError("signal_entry_invalid")
    if low == high:
        return (CriticalEntry(entry_index=1, order_type="market", price=low),)
    return ()


def envelope(entries: Iterable[CriticalEntry]) -> tuple[Decimal, Decimal]:
    values = tuple(item.price for item in entries)
    if not values:
        raise ValueError("entry_plan_empty")
    return min(values), max(values)


def augment_management_actions(
    raw_text: str,
    actions: Iterable[dict[str, str | None]],
) -> tuple[dict[str, str | None], ...]:
    """Preserve explicit partial and layer scope for the demo management boundary."""
    text = raw_text or ""
    result = [dict(action) for action in actions]

    close_layers = _CLOSE_LAYERS.search(text)
    if close_layers is not None:
        count = int(close_layers.group(1))
        if count > 0:
            target = (
                f"worst_{count}_layers"
                if _LEAVE_BEST.search(text)
                else f"first_{count}_layers"
            )
            result.insert(0, {"type": "close", "target": target, "value": None})

    layer_index: int | None = None
    if _SECOND_ENTRY_CONTEXT.search(text):
        layer_index = 2
    elif _FIRST_ENTRY_CONTEXT.search(text):
        layer_index = 1

    partial_command = _PARTIAL.search(text) is not None
    for action in result:
        action_type = str(action.get("type") or "")
        target = str(action.get("target") or "all").lower()

        if action_type == "cancel_pending":
            action["target"] = "pending_layers"
            continue

        if action_type == "close" and target == "tp1" and partial_command:
            action["target"] = (
                f"entry_{layer_index}_partial_tp1"
                if layer_index is not None
                else "partial_tp1"
            )
            continue

        if layer_index is not None and action_type in {
            "move_to_break_even",
            "edit_stop_loss",
        } and target in {"all", ""}:
            action["target"] = f"entry_{layer_index}"

    seen: set[tuple[str | None, str | None, str | None]] = set()
    deduped: list[dict[str, str | None]] = []
    for action in result:
        key = (action.get("type"), action.get("target"), action.get("value"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return tuple(deduped)


__all__ = [
    "CriticalEntry",
    "augment_management_actions",
    "envelope",
    "parse_critical_entries",
]
