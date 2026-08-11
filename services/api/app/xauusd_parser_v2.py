"""Real-source XAUUSD/GOLD parser learned from Testing traffic.

This extends the original Day 16 first-format parser without guessing missing
instructions. It recognises the concrete formats observed in TIG's Asia Trades,
TDC V2 and Matthew trades, preserving entry ranges, explicit pending-limit
wording and an explicit final TP OPEN runner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

PARSER_VERSION_V2 = "day16-v2-real-source-formats"

_PRICE_BODY = r"[0-9]+(?:[.,][0-9]+)?"


@dataclass(frozen=True, slots=True)
class ParsedXauusdTradeV2:
    symbol: str
    direction: str
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profits: tuple[Decimal | None, ...]
    size_multiplier: Decimal
    order_type: str

    @property
    def entry(self) -> Decimal:
        """Legacy convenience for exact-entry tests; range-aware code uses low/high."""
        return self.entry_low


@dataclass(frozen=True, slots=True)
class ParseResultV2:
    status: str
    reason: str
    matched_rules: tuple[str, ...]
    trade: ParsedXauusdTradeV2 | None = None


_EXACT_HEADERS = (
    re.compile(
        rf"^XAUUSD\s+(BUY|SELL)\s+(?:@\s*|ENTRY\s+)?({_PRICE_BODY})$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(BUY|SELL)\s+XAUUSD\s+(?:@\s*|ENTRY\s+)?({_PRICE_BODY})$",
        re.IGNORECASE,
    ),
)
_DIRECTION_INSTRUMENT = re.compile(r"^(BUY|SELL)\s+(XAUUSD|GOLD)$", re.IGNORECASE)
_GOLD_NOW = re.compile(r"^(BUY|SELL)\s+GOLD\s+NOW$", re.IGNORECASE)
_GOLD_AT = re.compile(
    rf"^(BUY|SELL)\s+(?:(LIMIT|LIMITS)\s+)?GOLD\s+@\s*"
    rf"({_PRICE_BODY})(?:\s*/\s*({_PRICE_BODY}))?(?:\s+AREA)?$",
    re.IGNORECASE,
)
_ENTRY = re.compile(rf"^ENTRY\s*[:=@-]?\s*({_PRICE_BODY})$", re.IGNORECASE)
_SECOND_ENTRY = re.compile(
    rf"^SECOND\s+ENTRY\s*[:=@-]?\s*({_PRICE_BODY})$",
    re.IGNORECASE,
)
_RANGE_ONLY = re.compile(
    rf"^({_PRICE_BODY})\s*(?:-|/)\s*({_PRICE_BODY})$",
    re.IGNORECASE,
)
_SL = re.compile(
    rf"^(?:SL|STOP\s*LOSS)\s*[:=@-]?\s*({_PRICE_BODY})$",
    re.IGNORECASE,
)
_TP_NUMBERED = re.compile(
    rf"^TP\s*#?\s*(?P<index>\d+)\s*[:=@-]?\s*"
    rf"(?P<value>OPEN|{_PRICE_BODY})$",
    re.IGNORECASE,
)
_TP_UNNUMBERED = re.compile(
    rf"^TP\s*[:=@-]?\s*(?P<value>OPEN|{_PRICE_BODY})$",
    re.IGNORECASE,
)
_DOUBLE_SIZE = re.compile(r"^USE\s+DOUBLE\s+LOT\s+SIZE$", re.IGNORECASE)
_PROVIDER_COMMENTARY = (
    re.compile(r"^MANAGE\s+RISK\s+PROPERLY[.!]?$", re.IGNORECASE),
    re.compile(r"^HIGH\s+RISK\s+TRADE[.!]?$", re.IGNORECASE),
)


def _decimal(value: str) -> Decimal | None:
    try:
        result = Decimal(value.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result <= 0:
        return None
    return result


def _lines(raw_text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in (raw_text or "").splitlines():
        if not raw_line.strip():
            continue
        line = " ".join(raw_line.replace("\u00a0", " ").strip().split())
        # Provider status emojis may prefix BUY/SELL. They carry no trade field.
        line = re.sub(
            r"^[^A-Za-z0-9]+(?=(?:BUY|SELL)\b)",
            "",
            line,
            flags=re.IGNORECASE,
        )
        lines.append(line)
    return lines


def _failed(reason: str, rule: str) -> ParseResultV2:
    return ParseResultV2("failed", reason, (rule,))


def parse_xauusd_trade_v2(raw_text: str) -> ParseResultV2:
    lines = _lines(raw_text)
    if not lines:
        return _failed("Message contains no trade text.", "empty_text")

    direction: str | None = None
    order_type = "market"
    entry_values: list[Decimal] = []
    first = lines[0]
    consumed_header = False
    gold_alias = False

    for pattern in _EXACT_HEADERS:
        match = pattern.fullmatch(first)
        if match:
            direction = match.group(1).upper()
            value = _decimal(match.group(2))
            if value is None:
                return _failed("Entry price is invalid.", "invalid_entry")
            entry_values.append(value)
            consumed_header = True
            break

    if not consumed_header:
        match = _GOLD_AT.fullmatch(first)
        if match:
            direction = match.group(1).upper()
            order_type = "pending" if match.group(2) else "market"
            first_entry = _decimal(match.group(3))
            second_entry = _decimal(match.group(4)) if match.group(4) else None
            if first_entry is None or (match.group(4) and second_entry is None):
                return _failed("Entry range is invalid.", "invalid_entry")
            entry_values.append(first_entry)
            if second_entry is not None:
                entry_values.append(second_entry)
            gold_alias = True
            consumed_header = True

    if not consumed_header:
        match = _DIRECTION_INSTRUMENT.fullmatch(first)
        if match:
            direction = match.group(1).upper()
            gold_alias = match.group(2).upper() == "GOLD"
            consumed_header = True

    if not consumed_header:
        match = _GOLD_NOW.fullmatch(first)
        if match:
            direction = match.group(1).upper()
            gold_alias = True
            consumed_header = True

    if not consumed_header or direction is None:
        return _failed(
            "First line must contain a recognised BUY/SELL XAUUSD or GOLD trade header.",
            "invalid_header",
        )

    stop_loss: Decimal | None = None
    take_profits: dict[int, Decimal | None] = {}
    double_size = False
    second_entry_seen = len(entry_values) > 1

    for line in lines[1:]:
        entry_match = _ENTRY.fullmatch(line)
        if entry_match:
            if entry_values:
                return _failed("Primary entry appears more than once.", "duplicate_entry")
            value = _decimal(entry_match.group(1))
            if value is None:
                return _failed("Entry price is invalid.", "invalid_entry")
            entry_values.append(value)
            continue

        second_match = _SECOND_ENTRY.fullmatch(line)
        if second_match:
            if not entry_values:
                return _failed("Second entry appeared before the primary entry.", "second_entry_without_primary")
            if second_entry_seen or len(entry_values) > 1:
                return _failed("Second entry appears more than once.", "duplicate_second_entry")
            value = _decimal(second_match.group(1))
            if value is None:
                return _failed("Second entry price is invalid.", "invalid_entry")
            entry_values.append(value)
            second_entry_seen = True
            continue

        range_match = _RANGE_ONLY.fullmatch(line)
        if range_match:
            if entry_values:
                return _failed("Entry range appears more than once.", "duplicate_entry")
            first_value = _decimal(range_match.group(1))
            second_value = _decimal(range_match.group(2))
            if first_value is None or second_value is None:
                return _failed("Entry range is invalid.", "invalid_entry")
            entry_values.extend((first_value, second_value))
            second_entry_seen = True
            continue

        sl_match = _SL.fullmatch(line)
        if sl_match:
            if stop_loss is not None:
                return _failed("Stop loss appears more than once.", "duplicate_stop_loss")
            stop_loss = _decimal(sl_match.group(1))
            if stop_loss is None:
                return _failed("Stop-loss price is invalid.", "invalid_stop_loss")
            continue

        tp_match = _TP_NUMBERED.fullmatch(line)
        if tp_match:
            index = int(tp_match.group("index"))
            if index <= 0:
                return _failed("Take-profit numbering must start at TP1.", "invalid_tp_index")
            if index in take_profits:
                return _failed(f"TP{index} appears more than once.", "duplicate_take_profit")
            token = tp_match.group("value")
            value = None if token.upper() == "OPEN" else _decimal(token)
            if token.upper() != "OPEN" and value is None:
                return _failed(f"TP{index} price is invalid.", "invalid_take_profit")
            take_profits[index] = value
            continue

        tp_match = _TP_UNNUMBERED.fullmatch(line)
        if tp_match:
            index = max(take_profits, default=0) + 1
            token = tp_match.group("value")
            value = None if token.upper() == "OPEN" else _decimal(token)
            if token.upper() != "OPEN" and value is None:
                return _failed(f"TP{index} price is invalid.", "invalid_take_profit")
            take_profits[index] = value
            continue

        if _DOUBLE_SIZE.fullmatch(line):
            if double_size:
                return _failed(
                    "Double-size instruction appears more than once.",
                    "duplicate_size_instruction",
                )
            double_size = True
            continue

        if any(pattern.fullmatch(line) for pattern in _PROVIDER_COMMENTARY):
            # These phrases are informational. They never alter user risk.
            continue

        return _failed(
            f"Unrecognised field in XAUUSD signal: {line}",
            "unrecognised_field",
        )

    if not entry_values:
        return _failed("Explicit entry price or entry range is required.", "missing_entry")
    if len(entry_values) > 2:
        return _failed("More than two explicit entry prices are not supported.", "too_many_entries")
    if stop_loss is None:
        return _failed("Explicit SL/STOP LOSS field is required.", "missing_stop_loss")
    if not take_profits:
        return _failed("At least TP1 is required.", "missing_take_profit")

    indexes = sorted(take_profits)
    expected = list(range(1, indexes[-1] + 1))
    if indexes != expected:
        return _failed(
            "Take-profit numbering must be contiguous from TP1 with no gaps.",
            "non_contiguous_take_profits",
        )

    ordered_targets = tuple(take_profits[index] for index in indexes)
    open_indexes = [index for index, target in enumerate(ordered_targets) if target is None]
    if open_indexes and open_indexes != [len(ordered_targets) - 1]:
        return _failed(
            "TP OPEN is supported only as the final provider target.",
            "open_target_not_final",
        )
    if all(target is None for target in ordered_targets):
        return _failed(
            "At least one numeric take-profit is required before TP OPEN.",
            "missing_numeric_take_profit",
        )

    entry_low = min(entry_values)
    entry_high = max(entry_values)
    trade = ParsedXauusdTradeV2(
        symbol="XAUUSD",
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        take_profits=ordered_targets,
        size_multiplier=Decimal("2") if double_size else Decimal("1"),
        order_type=order_type,
    )
    rules = ["xauusd", "direction", "entry", "stop_loss", "take_profits"]
    if gold_alias:
        rules.append("gold_alias")
    if entry_low != entry_high:
        rules.append("entry_range")
    if order_type == "pending":
        rules.append("pending_limit")
    if ordered_targets[-1] is None:
        rules.append("open_runner")
    if double_size:
        rules.append("double_lot_size")
    return ParseResultV2(
        "parsed",
        "Known real-source XAUUSD/GOLD signal format parsed successfully.",
        tuple(rules),
        trade,
    )
