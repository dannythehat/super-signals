"""Canonical fail-closed provider management policy.

OpenAI may identify which trade a provider is talking about, but broker mutations come
only from mechanically explicit current-message instructions defined here. Ambient
history may link a trade; it may never donate an action, price, TP index or side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class Day27ManagementPolicyResult:
    actions: tuple[dict[str, str | None], ...]
    reason: str


_OPTIONAL = re.compile(
    r"\b(?:IF\s+YOU\s+(?:WANT|WISH)|CONSIDER|UP\s+TO\s+YOU|YOUR\s+CHOICE)\b"
    r"|\b(?:BANK|TAKE)\s+(?:THE\s+)?BLUE\b.*\bOR\b"
    r"|\bOR\s+(?:(?:GO|MOVE|SET)\s+(?:(?:THE\s+)?(?:SL|STOP\s*LOSS)\s+)?(?:TO\s+)?)?(?:BE|BREAKEVEN|BREAK\s+EVEN)\b",
    re.IGNORECASE | re.DOTALL,
)
_RESULT_BE = re.compile(
    r"^\s*(?:I[’']?M|WE[’']?RE|TRADE\s+IS|POSITION\s+IS)\s+(?:AT\s+)?(?:BE|BREAKEVEN|BREAK\s+EVEN)\s*[.!✅🔥]*\s*$"
    r"|^\s*OUT\s+AT\s+(?:BE|BREAKEVEN|BREAK\s+EVEN)\s*[.!✅🔥]*\s*$",
    re.IGNORECASE,
)
_CLOSE_ALL = re.compile(
    r"\b(?:CLOSE(?:D)?\s+ALL|CLOSE\s+EVERYTHING|OUT\s+AT\s+ENTRY\s+ON\s+THE\s+REST(?:\s+OF\s+(?:MY|THE)\s+POSITION)?|OUT\s+ON\s+THE\s+REST)\b",
    re.IGNORECASE,
)
_EXIT_NOW = re.compile(
    r"\b(?:EXIT|CLOSE)\s+(?:IT|NOW|THE\s+(?:TRADE|POSITIONS?|LOT|BUY|SELL))\b"
    r"|\bCLOSE\s+(?:(?:OUR|MY|YOUR|THE|THIS)\s+)?(?:TRADE|SETUP|SET\s*UP)\b"
    r"|\bCLOSE\s+THIS\s+OUT\b"
    r"|\bCLOSE\s+OUT(?:\s+OVERALL)?\b"
    r"|\bCLOSING\s+OUT\s+NOW\b"
    r"|\bGET\s+OUT\s+(?:NOW|OF\s+(?:IT|THE\s+TRADE))\b"
    r"|\bGO\s+FLAT\b"
    r"|\bCLOSE\s+(?:YOUR|MY|ALL)?\s*(?:REMAINING|OPEN)\s+(?:TRADES?|POSITIONS?)\b",
    re.IGNORECASE,
)
_OUT_THIS_SETUP = re.compile(
    r"\b(?:WE\s*(?:['’]RE|ARE)\s+)?OUT\s+(?:OF\s+)?(?:THIS|THE)\s+"
    r"(?:SET\s*UP|SETUP|TRADE|POSITION)\b",
    re.IGNORECASE,
)
_CLOSE_NOW = re.compile(
    r"\bCLOSE\b.{0,70}\b(?:TRADE|POSITION|SET\s*UP|SETUP|BUY|SELL)\b.{0,30}\bNOW\b"
    r"|\bCLOSE\b.{0,30}\bNOW\b",
    re.IGNORECASE | re.DOTALL,
)
_OR_PROTECTIVE_CHOICE = re.compile(
    r"\bOR\b.{0,100}\b(?:BE|BREAKEVEN|BREAK\s+EVEN|RISK\s*[- ]?FREE)\b",
    re.IGNORECASE | re.DOTALL,
)
_TARGETED_OR_PARTIAL_CLOSE = re.compile(
    r"\bCLOSE\s+(?:THE\s+)?(?:TP\s*\d+|HALF|PARTIAL(?:LY)?|ONE|FIRST|SECOND|THIRD)\b",
    re.IGNORECASE,
)
_CLOSE_PROFIT = re.compile(
    r"\bCLOSE\s+(?:THE\s+)?PROFITS?\b"
    r"|\bCLOSE\s+PROFIT\s+WHEN\s+YOU\s+SEE\s+IT\b"
    r"|\bTAKE\s+PROFIT\s+WHEN\s+YOU\s+SEE\s+IT\b",
    re.IGNORECASE,
)
_CLOSE_LOSS = re.compile(
    r"\bCLOSE(?:\s+(?:IT|THE\s+(?:TRADE|POSITION)))?\s+"
    r"(?:WITH|AT|FOR|IN)\s+(?:(?:A|THE)\s+)?(?:SMALL\s+|MINOR\s+|TINY\s+)?LOSS(?:ES)?\b"
    r"|\bTAKE\s+(?:(?:THE|A)\s+)?(?:SMALL\s+|MINOR\s+|TINY\s+)?LOSS(?:ES)?\b"
    r"|\bCUT\s+(?:THE\s+)?LOSS(?:ES)?\b",
    re.IGNORECASE,
)
_CLOSE_NUMBERED = re.compile(
    r"\bCLOSE\s+(?:YOUR\s+)?(?:TP|POSITION)\s*([1-9]\d*)\b",
    re.IGNORECASE,
)
_CLOSE_FIRST_POSITION = re.compile(
    r"\bCLOSE\s+(?:YOUR\s+)?FIRST\s+(?:TP|POSITION)\b",
    re.IGNORECASE,
)
_CLOSE_FIRST_ENTRY = re.compile(
    r"\bCLOSE\s+(?:YOUR\s+)?FIRST\s+ENTR(?:Y|IES)\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(r"\bCANCEL(?:LED|ED|ING)?\b", re.IGNORECASE)
_REMOVE_PENDING = re.compile(
    r"\bREMOVE(?:D|ING)?\b.{0,60}"
    r"(?:\b(?:BUY|SELL)\s+(?:LIMIT|STOP)\b|\bPENDING(?:\s+ORDERS?)?\b)",
    re.IGNORECASE | re.DOTALL,
)
_OPEN_EXTRA = re.compile(
    r"(?im)^\s*OPEN\s+EXTRA\s+(?:GOLD|XAUUSD)\s+(BUYS?|SELLS?)\b"
)

_NUMERIC_STOP = re.compile(
    r"\b(?:MOVE|SET|CHANGE|UPDATE|PLACE)\s+"
    r"(?:ALL\s+)?(?:(?:THE|YOUR|MY|OUR)\s+)?(?:(?:GOLD|XAUUSD)\s+)?"
    r"(?:SL(?:S)?|STOP\s*LOSS(?:ES)?)\s+(?:BACK\s+)?(?:TO|AT)?\s*"
    r"(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_SL_PATTERNS = (
    _NUMERIC_STOP,
    re.compile(
        r"\b(?:SL|STOP\s*LOSS)\s+(?:IS\s+)?SET\s+TO\s+BE\s+AT\s+(\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(\d+(?:\.\d+)?)\s+(?:SL|STOP\s*LOSS)\s+TO\s+BE\b", re.IGNORECASE),
    re.compile(
        r"\bUSE\s+(\d+(?:\.\d+)?)\s+AS\s+(?:AN?\s+)?(?:SL|STOP\s*LOSS)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bRISK\s*[- ]?FREE+\s+(?:AT\s+|@\s*)?(\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
)
_TP_CHANGE = re.compile(
    r"\b(?:MOVE|SET|CHANGE|UPDATE)\s+(?:THE\s+)?TP\s*([1-9]\d*)\s*(?:TO|AT)?\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_MOVE_BE = re.compile(
    r"\b(?:MOVE|SET|PUT)\s+(?:ALL\s+)?(?:(?:THE|YOUR|MY|OUR)\s+)?"
    r"(?:(?:GOLD|XAUUSD)\s+)?(?:SL(?:S)?|STOP\s*LOSS(?:ES)?|STOP|STOPS)\s+"
    r"(?:BACK\s+)?(?:TO|AT)\s+(?:ENTRY|BE|BREAKEVEN|BREAK\s+EVEN)\b"
    r"|\b(?:SL(?:S)?|STOP\s*LOSS(?:ES)?|STOP|STOPS)\s+(?:BACK\s+)?(?:TO|AT)\s+(?:ENTRY|BE|BREAKEVEN|BREAK\s+EVEN)\b"
    r"|\b(?:MOVE|SET)\s+(?:TO\s+)?(?:FULLY\s+)?(?:BE|BREAKEVEN|BREAK\s+EVEN)\b"
    r"|^\s*(?:BE|BREAKEVEN|BREAK\s+EVEN)\s+NOW\s*[.!✅🔥]*\s*$"
    r"|\bBREAKEVEN\s+SET\b"
    r"|\bMAKE\s+(?:(?:YOUR|MY|THE)\s+)?(?:TRADE|SETUP|SET\s*UP|POSITION)\s+(?:OVERALL\s+)?RISK\s*[- ]?FREE\b"
    r"|\bI\s+WILL\s+MAKE\s+(?:MY|THE)\s+TRADE\s+RISK\s*[- ]?FREE\s+NOW\b"
    r"|\b(?:LOCK|LOCKING)\s+IN\s+(?:SOME\s+|THE\s+)?PROFITS?\b"
    r"|\b(?:SECURE|PROTECT)\s+(?:SOME\s+|THE\s+|YOUR\s+)?PROFITS?\b"
    r"|\bRISK\s*[- ]?FREE\s+(?:IT|NOW|THE\s+TRADE)\b",
    re.IGNORECASE,
)
_TP_HIT = re.compile(
    r"\bTP\s*(\d+)\b"
    r"(?:\s*(?:&|AND|,)\s*(?:TP\s*)?(\d+)\b)?"
    r"(?:\s*(?:&|AND|,)\s*(?:TP\s*)?(\d+)\b)?"
    r"\s*(?:(?:IS|ARE)\s+)?(?:BOTH\s+|ALL\s+)?HIT\b",
    re.IGNORECASE,
)
_TAKE_PARTIALS = re.compile(
    r"\b(?:TAKE|BOOK)\s+(?:SOME\s+|YOUR\s+|THE\s+|MAXIMUM\s+)?(?:PARTIALS?|PARTIALLY\s+PROFITS?|MORES?|PROFITS?)\b"
    r"|\bBOOK\s+PARTIAL\b|\bTAKE\s+PARTIAL\s+PROFITS?\b|\bCLOSE\s+PARTIALS?\b"
    r"|\b(?:CLOSE|BANK|SECURE|TAKE)\s+(?:OFF\s+)?HALF\b"
    r"|\bCLOSE\s+(?:SOME|A\s+PORTION)\s+(?:OF\s+)?(?:IT|THE\s+(?:TRADE|POSITIONS?))?\b"
    r"|\bBANK\s+(?:SOME|PART)\s+(?:OF\s+)?(?:IT|THE\s+PROFITS?)\b",
    re.IGNORECASE,
)
_FUTURE_INTENT = re.compile(
    r"\b(?:I|WE)\s*(?:['’]LL|WILL)\b|\bGOING\s+TO\b|\bAT\s+TP\s*\d"
    r"|\b(?:WHEN|ONCE)\s+(?:IT|PRICE|WE|TP\s*\d)",
    re.IGNORECASE,
)
_FUTURE_CONDITIONAL_BE = re.compile(
    r"\bAT\s+TP\s*\d\b.*\b(?:BE|BREAKEVEN|BREAK\s+EVEN|RISK\s*[- ]?FREE)\b"
    r"|\b(?:WHEN|ONCE)\b.*\b(?:BE|BREAKEVEN|BREAK\s+EVEN|RISK\s*[- ]?FREE)\b",
    re.IGNORECASE | re.DOTALL,
)
_OPTIONAL_PROTECTIVE = re.compile(
    r"\b(?:BE|BREAKEVEN|BREAK\s+EVEN)\b|\bRISK\s*[- ]?FREE\b"
    r"|\b(?:LOCK|LOCKING)\s+IN\b|\b(?:SECURE|PROTECT)\s+(?:SOME\s+|THE\s+|YOUR\s+)?PROFITS?\b",
    re.IGNORECASE,
)
_OPTIONAL_ENTRY = re.compile(
    r"\b(?:ENTER|ENTRY|ADD|BUY|SELL|LAYER|SCALE\s+IN)\b",
    re.IGNORECASE,
)


def _price(value: str) -> str | None:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return format(parsed.normalize(), "f")


def _dedupe(actions: list[dict[str, str | None]]) -> tuple[dict[str, str | None], ...]:
    seen: set[tuple[str | None, str | None, str | None]] = set()
    result: list[dict[str, str | None]] = []
    for action in actions:
        key = (action.get("type"), action.get("target"), action.get("value"))
        if key in seen:
            continue
        seen.add(key)
        result.append(action)
    return tuple(result)


def _decisive_close(text: str) -> bool:
    if _OUT_THIS_SETUP.search(text) or _CLOSE_LOSS.search(text):
        return True
    if _TARGETED_OR_PARTIAL_CLOSE.search(text):
        return False
    return _CLOSE_NOW.search(text) is not None and _OR_PROTECTIVE_CHOICE.search(text) is None


def _extract_actions(text: str) -> list[dict[str, str | None]]:
    actions: list[dict[str, str | None]] = []

    # Profit-qualified language is never an unconditional close. The management
    # executor must inspect current broker floating P/L and close only profitable
    # mapped positions.
    if _CLOSE_PROFIT.search(text):
        actions.append({"type": "close", "target": "profitable_only", "value": None})
    elif _CLOSE_ALL.search(text) or _EXIT_NOW.search(text) or _decisive_close(text):
        actions.append({"type": "close", "target": "all", "value": None})
    else:
        for match in _CLOSE_NUMBERED.finditer(text):
            actions.append({"type": "close", "target": f"TP{match.group(1)}", "value": None})
        if _CLOSE_FIRST_POSITION.search(text) or (
            _TAKE_PARTIALS.search(text) and not _FUTURE_INTENT.search(text)
        ):
            actions.append({"type": "close", "target": "TP1", "value": None})
        if _CLOSE_FIRST_ENTRY.search(text):
            actions.append({"type": "close", "target": "entry_1", "value": None})

    numeric_sl_found = False
    for pattern in _SL_PATTERNS:
        for match in pattern.finditer(text):
            value = _price(match.group(1))
            if value is not None:
                actions.append({"type": "edit_stop_loss", "target": "all", "value": value})
                numeric_sl_found = True

    for match in _TP_CHANGE.finditer(text):
        value = _price(match.group(2))
        if value is not None:
            actions.append({"type": "edit_take_profit", "target": f"TP{match.group(1)}", "value": value})

    protective = numeric_sl_found or bool(
        _MOVE_BE.search(text) and not _FUTURE_CONDITIONAL_BE.search(text)
    )
    if not numeric_sl_found and protective:
        actions.append({"type": "move_to_break_even", "target": "all", "value": None})

    # A provider result such as "TP1 HIT" is evidence only by itself. In the same
    # message as an explicit protective instruction it becomes a compound management
    # command: resolve the named TP legs first, then protect what remains.
    if protective:
        tp_actions: list[dict[str, str | None]] = []
        for match in _TP_HIT.finditer(text):
            for raw_index in match.groups():
                if raw_index:
                    tp_actions.append({"type": "close", "target": f"TP{int(raw_index)}", "value": None})
        if tp_actions:
            actions = tp_actions + actions

    if _CANCEL.search(text) or _REMOVE_PENDING.search(text):
        actions.append({"type": "cancel_pending", "target": "all", "value": None})

    add = _OPEN_EXTRA.search(text)
    if add is not None:
        word = add.group(1).upper()
        side = "BUY" if word.startswith("BUY") else "SELL"
        actions.append({"type": "add_market", "target": "same_trade", "value": side})

    return actions


def extract_day27_management_actions(raw_text: str) -> Day27ManagementPolicyResult:
    """Extract every mechanically explicit broker-management action from this post."""
    text = (raw_text or "").strip()
    if not text:
        return Day27ManagementPolicyResult((), "unsupported_management")
    if _RESULT_BE.fullmatch(text):
        return Day27ManagementPolicyResult((), "provider_result_only")

    optional = _OPTIONAL.search(text) is not None
    actions = _dedupe(_extract_actions(text))

    if optional:
        # A softened standalone suggestion ("make risk free if you want") is not
        # broker authority. Preserve an exact numeric stop, but never promote the
        # generic suggestion to compulsory breakeven. Automatic breakeven is handled
        # separately after configured TP milestones are broker-confirmed.
        standalone_softener = re.search(
            r"\bIF\s+YOU\s+(?:WANT|WISH)\b", text, re.IGNORECASE
        ) is not None and re.search(r"\bOR\b", text, re.IGNORECASE) is None
        explicit_numeric_stops = [
            action
            for action in actions
            if action.get("type") == "edit_stop_loss" and action.get("value") is not None
        ]
        if standalone_softener and not _decisive_close(text):
            if explicit_numeric_stops:
                return Day27ManagementPolicyResult(
                    _dedupe(explicit_numeric_stops),
                    "explicit_numeric_stop_within_optional_message",
                )
            return Day27ManagementPolicyResult((), "optional_management_instruction")

        # Preserve the established handling for an explicit close command followed by
        # an optional hold clause, and for a literal close-or-BE choice.
        explicit = [
            action
            for action in actions
            if action.get("type") != "close"
            or action.get("target") == "profitable_only"
            or _decisive_close(text)
        ]
        if explicit:
            return Day27ManagementPolicyResult(
                _dedupe(explicit), "explicit_instruction_within_optional_message"
            )
        if _OPTIONAL_PROTECTIVE.search(text) and not _OPTIONAL_ENTRY.search(text):
            return Day27ManagementPolicyResult(
                ({"type": "move_to_break_even", "target": "all", "value": None},),
                "optional_protective_resolved_to_breakeven",
            )
        return Day27ManagementPolicyResult((), "optional_management_instruction")

    if not actions:
        return Day27ManagementPolicyResult((), "unsupported_management")
    if any(action.get("type") == "add_market" for action in actions):
        return Day27ManagementPolicyResult(actions, "explicit_active_trade_add_entry")
    if any(action.get("target") == "profitable_only" for action in actions):
        return Day27ManagementPolicyResult(actions, "profit_qualified_close")
    if _OUT_THIS_SETUP.search(text) or _decisive_close(text):
        return Day27ManagementPolicyResult(actions, "explicit_literal_close")
    if protective := any(
        action.get("type") in {"edit_stop_loss", "move_to_break_even"} for action in actions
    ):
        if protective and _TP_HIT.search(text):
            return Day27ManagementPolicyResult(actions, "compound_tp_hit_and_protect")
    return Day27ManagementPolicyResult(actions, "day27_explicit_management")


__all__ = ["Day27ManagementPolicyResult", "extract_day27_management_actions"]
