"""Day 16 deterministic parser for the first XAUUSD signal format.

This module extracts structured parser evidence only. It never creates a
Signal, Position, lot size, publication, or broker action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

PARSER_VERSION = "day16-xauusd-v1"


@dataclass(frozen=True, slots=True)
class ParsedXauusdTrade:
    symbol: str
    direction: str
    entry: Decimal
    stop_loss: Decimal
    take_profits: tuple[Decimal, ...]
    size_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class ParseResult:
    status: str
    reason: str
    matched_rules: tuple[str, ...]
    trade: ParsedXauusdTrade | None = None


_PRICE = r"([0-9]+(?:[.,][0-9]+)?)"
_HEADER_PATTERNS = (
    re.compile(rf"^XAUUSD\s+(BUY|SELL)\s+(?:@\s*|ENTRY\s+)?{_PRICE}$", re.IGNORECASE),
    re.compile(rf"^(BUY|SELL)\s+XAUUSD\s+(?:@\s*|ENTRY\s+)?{_PRICE}$", re.IGNORECASE),
)
_SL = re.compile(rf"^(?:SL|STOP\s*LOSS)\s*[:=@-]?\s*{_PRICE}$", re.IGNORECASE)
_TP = re.compile(rf"^TP\s*#?\s*(\d+)\s*[:=@-]?\s*{_PRICE}$", re.IGNORECASE)
_DOUBLE_SIZE = re.compile(r"^USE\s+DOUBLE\s+LOT\s+SIZE$", re.IGNORECASE)


def _decimal(value: str) -> Decimal | None:
    try:
        result = Decimal(value.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result <= 0:
        return None
    return result


def _lines(raw_text: str) -> list[str]:
    return [" ".join(line.replace("\u00a0", " ").strip().split()) for line in (raw_text or "").splitlines() if line.strip()]


def parse_xauusd_trade(raw_text: str) -> ParseResult:
    """Parse the locked Day 16 XAUUSD format without inventing missing values."""

    lines = _lines(raw_text)
    if not lines:
        return ParseResult("failed", "Message contains no trade text.", ("empty_text",))

    header_match = None
    for pattern in _HEADER_PATTERNS:
        header_match = pattern.fullmatch(lines[0])
        if header_match:
            break
    if header_match is None:
        return ParseResult(
            "failed",
            "First line must contain XAUUSD, exactly one BUY/SELL direction and one entry price.",
            ("invalid_header",),
        )

    direction = header_match.group(1).upper()
    entry = _decimal(header_match.group(2))
    if entry is None:
        return ParseResult("failed", "Entry price is invalid.", ("invalid_entry",))

    stop_loss: Decimal | None = None
    take_profits: dict[int, Decimal] = {}
    double_size = False

    for line in lines[1:]:
        sl_match = _SL.fullmatch(line)
        if sl_match:
            if stop_loss is not None:
                return ParseResult("failed", "Stop loss appears more than once.", ("duplicate_stop_loss",))
            stop_loss = _decimal(sl_match.group(1))
            if stop_loss is None:
                return ParseResult("failed", "Stop-loss price is invalid.", ("invalid_stop_loss",))
            continue

        tp_match = _TP.fullmatch(line)
        if tp_match:
            index = int(tp_match.group(1))
            if index <= 0:
                return ParseResult("failed", "Take-profit numbering must start at TP1.", ("invalid_tp_index",))
            if index in take_profits:
                return ParseResult("failed", f"TP{index} appears more than once.", ("duplicate_take_profit",))
            value = _decimal(tp_match.group(2))
            if value is None:
                return ParseResult("failed", f"TP{index} price is invalid.", ("invalid_take_profit",))
            take_profits[index] = value
            continue

        if _DOUBLE_SIZE.fullmatch(line):
            if double_size:
                return ParseResult("failed", "Double-size instruction appears more than once.", ("duplicate_size_instruction",))
            double_size = True
            continue

        return ParseResult(
            "failed",
            f"Unrecognised field in XAUUSD signal: {line}",
            ("unrecognised_field",),
        )

    if stop_loss is None:
        return ParseResult("failed", "Explicit SL/STOP LOSS field is required.", ("missing_stop_loss",))
    if not take_profits:
        return ParseResult("failed", "At least TP1 is required.", ("missing_take_profit",))

    indexes = sorted(take_profits)
    expected = list(range(1, indexes[-1] + 1))
    if indexes != expected:
        return ParseResult(
            "failed",
            "Take-profit numbering must be contiguous from TP1 with no gaps.",
            ("non_contiguous_take_profits",),
        )

    trade = ParsedXauusdTrade(
        symbol="XAUUSD",
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=tuple(take_profits[index] for index in indexes),
        size_multiplier=Decimal("2") if double_size else Decimal("1"),
    )
    rules = ["xauusd", "direction", "entry", "stop_loss", "take_profits"]
    if double_size:
        rules.append("double_lot_size")
    return ParseResult(
        "parsed",
        "Known XAUUSD signal format parsed successfully.",
        tuple(rules),
        trade,
    )
