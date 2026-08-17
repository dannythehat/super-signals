"""Deterministic pending/layer interpretation for critical trade infrastructure.

The AI supervisor may understand provider dialect, but broker structure is derived here
from literal current-message evidence only. No price, SL or TP is borrowed from ambient
history. Explicit multi-entry setups are represented before execution so risk can be
shared across the declared layers instead of being silently multiplied.

TDC's repeated HIGH RISK TRADE template is a proven layered-zone dialect. In that
specific template, one XAUUSD price unit is one declared layer step (10 provider pips).
Plural LIMITS/STOPS create broker-held pending layers. A plain BUY/SELL zone creates an
immediate first layer plus retracement pending layers. Other ambiguous zones still fail
closed; this module never invents a grid for an unknown provider format.
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


_GRID_STEP = Decimal("1")
_MAX_GRID_LAYERS = 12
_TDC_LAYER_TEMPLATE = re.compile(
    r"(?im)^\s*(BUY|SELL)(\s+(?:LIMITS?|STOPS?))?\s+(?:XAUUSD|GOLD)\s*"
    r"@\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?:\s+AREA)?\s*$"
)
_HIGH_RISK = re.compile(r"\bHIGH\s+RISK\s+TRADE\b", re.IGNORECASE)
_TP_OPEN = re.compile(r"\bTP\s*(?:\d+\s*)?OPEN\b", re.IGNORECASE)
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
_CLOSE_LAYERS = re.compile(r"\bCLOSE\s+(\d+)\s+LAYERS?\b", re.IGNORECASE)
_LEAVE_BEST = re.compile(
    r"\b(?:LEAVE|KEEP)\s+(?:THE\s+)?BEST(?:\s+(?:ENTRY|LAYER|ONE))?\s+(?:RUNNING|OPEN)\b"
    r"|\bBEST\s+ENTRY\s+STILL\s+RUNNING\b",
    re.IGNORECASE,
)
_KEEP_BEST_SIDE = re.compile(
    r"\bKEEP\s+(?:THE\s+)?(?:LOWEST|HIGHEST|BEST)\s+ENTR(?:Y|IES)\s+RISK\s*[- ]?FREE\b",
    re.IGNORECASE,
)
_SECOND_ENTRY_CONTEXT = re.compile(r"\b(?:SECOND|2ND)\s+ENTRY\b|\bENTRY\s*2\b", re.IGNORECASE)
_FIRST_ENTRY_CONTEXT = re.compile(r"\bFIRST\s+ENTRY\b|\bENTRY\s*1\b", re.IGNORECASE)
_PARTIAL = re.compile(
    r"\b(?:BOOK|TAKE|CLOSE|BANK|SECURE)\b[^\n]{0,35}"
    r"\b(?:PARTIALS?|HALF|SOME\s+PROFIT|PROFIT\s+OFF)\b",
    re.IGNORECASE,
)
_RISK_FREE_PRICE = re.compile(
    r"\bRISK\s*[- ]?FREE+\s+(?:AT\s+|@\s*)?(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_CLOSE_PRICE_ROW = re.compile(
    r"(?im)^\s*(\d+(?:\.\d+)?)\s+CLOSE(?:\s+[+-]?\d+(?:\.\d+)?(?:\s*PIPS?)?)?\s*$"
)


def _price(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _format_price(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _pending_type(side: str, word: str) -> str:
    return f"{side.lower()}_{word.lower()}"


def _derived_layer_type(side: str, *, first: Decimal, later: Decimal) -> str:
    normalized = side.strip().upper()
    if normalized == "BUY":
        return "buy_limit" if later < first else "buy_stop"
    if normalized == "SELL":
        return "sell_limit" if later > first else "sell_stop"
    raise ValueError("trade_side_invalid")


def _tdc_layer_grid(text: str, normalized_side: str) -> tuple[CriticalEntry, ...] | None:
    match = _TDC_LAYER_TEMPLATE.search(text)
    if match is None:
        return None
    if _HIGH_RISK.search(text) is None or _TP_OPEN.search(text) is None:
        return None
    provider_side = match.group(1).upper()
    if provider_side != normalized_side:
        raise ValueError("pending_side_mismatch")
    first = _price(match.group(3))
    last = _price(match.group(4))
    if first is None or last is None or first == last:
        raise ValueError("layer_entry_invalid")
    if normalized_side == "BUY" and last >= first:
        raise ValueError("layer_grid_direction_invalid")
    if normalized_side == "SELL" and last <= first:
        raise ValueError("layer_grid_direction_invalid")
    distance = abs(first - last)
    quotient = distance / _GRID_STEP
    if quotient != quotient.to_integral_value():
        raise ValueError("layer_grid_step_ambiguous")
    count = int(quotient) + 1
    if count < 2 or count > _MAX_GRID_LAYERS:
        raise ValueError("layer_grid_size_invalid")
    direction = Decimal("-1") if normalized_side == "BUY" else Decimal("1")
    plural_pending = bool(match.group(2))
    entries: list[CriticalEntry] = []
    for offset in range(count):
        value = first + direction * _GRID_STEP * Decimal(offset)
        if plural_pending:
            order_type = "buy_limit" if normalized_side == "BUY" else "sell_limit"
        elif offset == 0:
            order_type = "market"
        else:
            order_type = _derived_layer_type(normalized_side, first=first, later=value)
        entries.append(CriticalEntry(offset + 1, order_type, value))
    return tuple(entries)


def _plain_entry_fallback(
    entry_low: object, entry_high: object
) -> tuple[CriticalEntry, ...]:
    """Treat the message as an ordinary (non-layered) signal.

    This is the pre-Day-43 V1 behavior: an entry/zone that the layering dialect
    cannot unambiguously interpret is not a reason to skip an otherwise complete
    signal. It falls back to the exact/zone entry already extracted upstream.
    """
    low = _price(entry_low)
    high = _price(entry_high)
    if low is None or high is None:
        raise ValueError("signal_entry_invalid")
    if low == high:
        return (CriticalEntry(entry_index=1, order_type="market", price=low),)
    return ()


def parse_critical_entries(
    raw_text: str,
    *,
    side: str,
    entry_low: object,
    entry_high: object,
) -> tuple[CriticalEntry, ...]:
    text = raw_text or ""
    normalized_side = side.strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("trade_side_invalid")
    proven_grid = _tdc_layer_grid(text, normalized_side)
    if proven_grid is not None:
        return proven_grid
    if _AMBIGUOUS_PENDING_ZONE.search(text) is not None:
        # Plural LIMIT/STOP wording next to a zone that is not the proven TDC
        # HIGH RISK TRADE template used to simply be read as an ordinary zone
        # entry (V1 never supported pending orders). Keep that safe behavior
        # instead of skipping an otherwise complete, executable signal.
        return _plain_entry_fallback(entry_low, entry_high)
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
        # V1 has always executed the first entry only and ignored "Second
        # entry" wording. When the first entry cannot be matched in the new
        # layering grammar (e.g. "BUY GOLD @4385" instead of "ENTRY 4385"),
        # or the two entries cannot be resolved into a valid layer pair, fall
        # back to that original ignore-second-entry behavior rather than
        # skipping a signal that was otherwise complete.
        first = _price(first_match.group(1)) if first_match is not None else None
        second = _price(second_match.group(1))
        if first is None or second is None or first == second:
            return _plain_entry_fallback(entry_low, entry_high)
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
                # An unresolvable extra layer does not invalidate the already
                # confirmed first/second layers; just omit it.
                continue
            entries.append(
                CriticalEntry(
                    entry_index=index,
                    order_type=_derived_layer_type(normalized_side, first=first, later=later),
                    price=later,
                )
            )
        return tuple(sorted(entries, key=lambda item: item.entry_index))
    return _plain_entry_fallback(entry_low, entry_high)


def envelope(entries: Iterable[CriticalEntry]) -> tuple[Decimal, Decimal]:
    values = tuple(item.price for item in entries)
    if not values:
        raise ValueError("entry_plan_empty")
    return min(values), max(values)


def augment_management_actions(
    raw_text: str,
    actions: Iterable[dict[str, str | None]],
) -> tuple[dict[str, str | None], ...]:
    """Preserve explicit partial, layer and TDC risk-free scope for paper management."""
    text = raw_text or ""
    result = [dict(action) for action in actions]
    close_prices: list[Decimal] = []
    for match in _CLOSE_PRICE_ROW.finditer(text):
        value = _price(match.group(1))
        if value is not None and value not in close_prices:
            close_prices.append(value)
    for value in reversed(close_prices):
        result.insert(
            0,
            {"type": "close", "target": f"entry_price_{_format_price(value)}", "value": None},
        )
    close_layers = _CLOSE_LAYERS.search(text)
    if close_layers is not None:
        count = int(close_layers.group(1))
        if count > 0:
            target = f"worst_{count}_layers" if _LEAVE_BEST.search(text) else f"first_{count}_layers"
            result.insert(0, {"type": "close", "target": target, "value": None})
    layer_index: int | None = None
    if _SECOND_ENTRY_CONTEXT.search(text):
        layer_index = 2
    elif _FIRST_ENTRY_CONTEXT.search(text):
        layer_index = 1
    partial_command = _PARTIAL.search(text) is not None
    risk_free = _RISK_FREE_PRICE.search(text)
    leave_best = _LEAVE_BEST.search(text) is not None
    keep_best_side = _KEEP_BEST_SIDE.search(text) is not None
    risk_free_price = _price(risk_free.group(1)) if risk_free is not None else None
    if risk_free_price is not None and not close_prices and not partial_command:
        result.insert(0, {"type": "close", "target": "all_but_best", "value": None})
    for action in result:
        action_type = str(action.get("type") or "")
        target = str(action.get("target") or "all").lower()
        if action_type == "cancel_pending":
            action["target"] = "pending_layers"
            continue
        if action_type == "close" and target == "tp1" and partial_command:
            action["target"] = (
                f"entry_{layer_index}_partial_tp1" if layer_index is not None else "partial_tp1"
            )
            continue
        if action_type in {"move_to_break_even", "edit_stop_loss"} and target in {"all", ""}:
            if layer_index is not None:
                action["target"] = f"entry_{layer_index}"
            elif risk_free_price is not None:
                action["target"] = f"best_entry_risk_free_{_format_price(risk_free_price)}"
            elif leave_best:
                action["target"] = "best_entry"
            elif close_prices or keep_best_side:
                action["target"] = "remaining"
    if leave_best and not any(
        action.get("type") == "close" and action.get("target") == "all_but_best"
        for action in result
    ):
        insert_at = 0
        while insert_at < len(result) and result[insert_at].get("type") == "close":
            insert_at += 1
        result.insert(insert_at, {"type": "close", "target": "all_but_best", "value": None})
    seen: set[tuple[str | None, str | None, str | None]] = set()
    deduped: list[dict[str, str | None]] = []
    for action in result:
        key = (action.get("type"), action.get("target"), action.get("value"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(action)
    return tuple(deduped)


__all__ = ["CriticalEntry", "augment_management_actions", "envelope", "parse_critical_entries"]
