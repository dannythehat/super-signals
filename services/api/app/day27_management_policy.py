"""Fail-closed provider follow-up extraction for Day 27.

The AI supervisor remains responsible for semantic understanding and lifecycle linking.
This module decides which management instructions are mechanically explicit enough to
be sent to the broker. It reads only the current provider message: ambient history may
identify the trade, but may never donate a management action or numeric value.
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

_SL_PATTERNS = (
    re.compile(
        r"\b(?:MOVE|SET|CHANGE|UPDATE)\s+(?:THE\s+)?(?:SL|STOP\s*LOSS)\s*(?:TO|AT)?\s*(\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:SL|STOP\s*LOSS)\s+(?:IS\s+)?SET\s+TO\s+BE\s+AT\s+(\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(\d+(?:\.\d+)?)\s+(?:SL|STOP\s*LOSS)\s+TO\s+BE\b", re.IGNORECASE),
    re.compile(
        r"\bUSE\s+(\d+(?:\.\d+)?)\s+AS\s+(?:AN?\s+)?(?:SL|STOP\s*LOSS)\b",
        re.IGNORECASE,
    ),
    # TDC dialect, observed live: "+20 / RISK FREE 4324" means move the stop to
    # 4324. The price is explicit, so it must win over a generic breakeven.
    re.compile(
        r"\bRISK\s*[- ]?FREE\s+(?:AT\s+|@\s*)?(\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    ),
)
_TP_CHANGE = re.compile(
    r"\b(?:MOVE|SET|CHANGE|UPDATE)\s+(?:THE\s+)?TP\s*([1-9]\d*)\s*(?:TO|AT)?\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_MOVE_BE = re.compile(
    r"\b(?:MOVE|SET)\s+(?:THE\s+)?(?:SL|STOP\s*LOSS)\s+TO\s+(?:BE|BREAKEVEN|BREAK\s+EVEN)\b"
    # Observed GTMO/David dialects: "set breakeven now", "set fully breakeven",
    # "Set Break Even". A leading MOVE/SET makes this an instruction; result-only
    # wording such as "I'm at BE" is still protected by _RESULT_BE below.
    r"|\b(?:MOVE|SET)\s+(?:TO\s+)?(?:FULLY\s+)?(?:BE|BREAKEVEN|BREAK\s+EVEN)\b"
    r"|^\s*(?:BE|BREAKEVEN|BREAK\s+EVEN)\s+NOW\s*[.!✅🔥]*\s*$"
    r"|\bBREAKEVEN\s+SET\b"
    r"|\bMAKE\s+(?:(?:YOUR|MY|THE)\s+)?(?:TRADE|SETUP|SET\s*UP|POSITION)\s+(?:OVERALL\s+)?RISK\s*[- ]?FREE\b"
    r"|\bI\s+WILL\s+MAKE\s+(?:MY|THE)\s+TRADE\s+RISK\s*[- ]?FREE\s+NOW\b"
    # Owner rule: protective wording moves the stop to breakeven.
    r"|\b(?:MOVE|SET|PUT)\s+(?:THE\s+)?(?:SL|STOP\s*LOSS|STOP|STOPS)\s+(?:TO|AT)\s+ENTRY\b"
    r"|\b(?:LOCK|LOCKING)\s+IN\s+(?:SOME\s+|THE\s+)?PROFITS?\b"
    r"|\b(?:SECURE|PROTECT)\s+(?:SOME\s+|THE\s+|YOUR\s+)?PROFITS?\b"
    r"|\bRISK\s*[- ]?FREE\s+(?:IT|NOW|THE\s+TRADE)\b",
    re.IGNORECASE,
)

# Partial-taking wording. Deliberately requires a partial sense: a bare "close"
# must not land here, and "close all" is matched earlier and wins.
_TAKE_PARTIALS = re.compile(
    r"\b(?:TAKE|BOOK)\s+(?:SOME\s+|YOUR\s+|THE\s+|MAXIMUM\s+)?(?:PARTIALS?|PARTIALLY\s+PROFITS?|MORES?|PROFITS?)\b"
    r"|\bBOOK\s+PARTIAL\b"
    r"|\bTAKE\s+PARTIAL\s+PROFITS?\b"
    # SureShot's observed command is "XAUUSD CLOSE PARTIAL ...".
    r"|\bCLOSE\s+PARTIALS?\b"
    r"|\b(?:CLOSE|BANK|SECURE|TAKE)\s+(?:OFF\s+)?HALF\b"
    r"|\bCLOSE\s+(?:SOME|A\s+PORTION)\s+(?:OF\s+)?(?:IT|THE\s+(?:TRADE|POSITIONS?))?\b"
    r"|\bBANK\s+(?:SOME|PART)\s+(?:OF\s+)?(?:IT|THE\s+PROFITS?)\b",
    re.IGNORECASE,
)

# Statements of future intent are not instructions. "At TP2 I'll close half" tells
# you what the provider plans to do later; acting on it closes a leg now, against a
# message that instructed nothing. This guard is why partial wording alone is never
# enough to act on.
_FUTURE_INTENT = re.compile(
    r"\b(?:I|WE)\s*(?:['’]LL|WILL)\b"
    r"|\bGOING\s+TO\b"
    r"|\bAT\s+TP\s*\d"
    r"|\b(?:WHEN|ONCE)\s+(?:IT|PRICE|WE|TP\s*\d)",
    re.IGNORECASE,
)

# A future trigger must never be promoted into an immediate protective broker action.
# Keep this narrower than _FUTURE_INTENT so an explicit current instruction such as
# "I will make my trade risk free now" remains executable, while "At TP2 ... set
# breakeven" and "when price reaches ... move to BE" remain evidence only.
_FUTURE_CONDITIONAL_BE = re.compile(
    r"\bAT\s+TP\s*\d\b.*\b(?:BE|BREAKEVEN|BREAK\s+EVEN|RISK\s*[- ]?FREE)\b"
    r"|\b(?:WHEN|ONCE)\b.*\b(?:BE|BREAKEVEN|BREAK\s+EVEN|RISK\s*[- ]?FREE)\b",
    re.IGNORECASE | re.DOTALL,
)

# Exit wording that means "get out of the trade" without relying on one exact
# provider dialect. These are imperative/current-action forms only. Past-result
# wording such as "all positions closed" or "I've closed out" deliberately does not
# match, so broker truth remains the authority for provider result statements.
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

# Optional wording that still concerns protecting an open trade. The Owner's rule is
# that ambiguity here resolves to the cautious action rather than to doing nothing:
# moving the stop to breakeven removes downside while leaving a winner running.
# Deliberately narrow: it requires a breakeven/risk-free/profit-protection outcome to
# be named. Optional wording about ENTERING a trade is never made executable by this.
_OPTIONAL_PROTECTIVE = re.compile(
    r"\b(?:BE|BREAKEVEN|BREAK\s+EVEN)\b"
    r"|\bRISK\s*[- ]?FREE\b"
    r"|\b(?:LOCK|LOCKING)\s+IN\b"
    r"|\b(?:SECURE|PROTECT)\s+(?:SOME\s+|THE\s+|YOUR\s+)?PROFITS?\b",
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


def _extract_actions(text: str) -> list[dict[str, str | None]]:
    """Pull every mechanically explicit management action out of one message."""
    actions: list[dict[str, str | None]] = []

    # Close actions are first because a combined message such as "close TP1 and move
    # SL to BE" must close the intended leg before modifying the surviving positions.
    if _CLOSE_ALL.search(text) or _EXIT_NOW.search(text):
        actions.append({"type": "close", "target": "all", "value": None})
    else:
        for match in _CLOSE_NUMBERED.finditer(text):
            actions.append({"type": "close", "target": f"TP{match.group(1)}", "value": None})
        if _CLOSE_FIRST_POSITION.search(text) or (
            _TAKE_PARTIALS.search(text) and not _FUTURE_INTENT.search(text)
        ):
            # Owner reading, 14 Aug 2026: "take partials" and "close half" are written
            # for followers holding a single position, and mean bank some now and let
            # the rest run. Super Signals opens one position per TP level, so the
            # equivalent is closing the nearest leg and leaving the others open. That
            # needs no partial-volume close: it is the same action as "close your
            # first position", which providers already say.
            actions.append({"type": "close", "target": "TP1", "value": None})
        if _CLOSE_FIRST_ENTRY.search(text):
            # Day 26 V1 opens only one supported entry layer. Keep the semantic target
            # distinct so future layered-entry support does not silently change this rule.
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
            actions.append(
                {"type": "edit_take_profit", "target": f"TP{match.group(1)}", "value": value}
            )

    if (
        not numeric_sl_found
        and _MOVE_BE.search(text)
        and not _FUTURE_CONDITIONAL_BE.search(text)
    ):
        actions.append({"type": "move_to_break_even", "target": "all", "value": None})

    if _CANCEL.search(text):
        actions.append({"type": "cancel_pending", "target": "all", "value": None})
    return actions


def extract_day27_management_actions(raw_text: str) -> Day27ManagementPolicyResult:
    """Extract mechanically explicit Day 27 broker-management actions.

    Optional/choice language is deliberately ignored rather than converted into a
    broker action. Provider result statements such as "I'm at BE" are also evidence,
    not instructions.
    """
    text = (raw_text or "").strip()
    if not text:
        return Day27ManagementPolicyResult((), "unsupported_management")
    optional = _OPTIONAL.search(text) is not None
    if optional:
        # An explicit instruction in the same message must survive optional wording.
        #
        # Observed live, TIG message 447:
        #     "Trade is running +40pips from best entry
        #      Move SL to 4314
        #      Making Second entry Risk Free if you want team"
        #
        # That contains a precise "Move SL to 4314". The optional check used to
        # return before extraction ran, so the explicit stop was discarded because
        # a later sentence happened to say "if you want". The Blueprint already
        # requires the opposite: a combined message such as "TP1 hit, move SL to
        # 4385" must still produce the explicit action.
        #
        # Extract first. Explicit stop, target and breakeven instructions are kept.
        # Close actions are deliberately dropped here: exiting a trade is not
        # something to infer from a sentence offering a choice.
        explicit = [
            action
            for action in _extract_actions(text)
            if action.get("type") != "close"
        ]
        if explicit:
            return Day27ManagementPolicyResult(
                _dedupe(explicit), "explicit_instruction_within_optional_message"
            )

        # Owner rule, 14 Aug 2026: with no explicit instruction to follow, a choice
        # about protecting an open trade takes the cautious option rather than doing
        # nothing. Breakeven removes the downside while leaving a winner running.
        #
        # Narrow on purpose: only when the optional sentence names a protective
        # outcome, and never when it concerns entering or adding to a position.
        if _OPTIONAL_PROTECTIVE.search(text) and not _OPTIONAL_ENTRY.search(text):
            return Day27ManagementPolicyResult(
                ({"type": "move_to_break_even", "target": "all", "value": None},),
                "optional_protective_resolved_to_breakeven",
            )
        return Day27ManagementPolicyResult((), "optional_management_instruction")
    if _RESULT_BE.fullmatch(text):
        return Day27ManagementPolicyResult((), "provider_result_only")

    deduped = _dedupe(_extract_actions(text))
    if not deduped:
        return Day27ManagementPolicyResult((), "unsupported_management")
    return Day27ManagementPolicyResult(deduped, "day27_explicit_management")


__all__ = ["Day27ManagementPolicyResult", "extract_day27_management_actions"]
