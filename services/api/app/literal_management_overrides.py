"""Mechanical management grammar corrections for explicit provider instructions.

These are not AI inferences. They cover literal imperative phrases that the Day27
policy intended to support but missed because of harmless words such as "your",
"gold" or "back". The wrapper runs the existing policy first and only fills a gap when
that policy found no action.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.day27_management_policy import Day27ManagementPolicyResult

_NUMERIC_STOP = re.compile(
    r"\b(?:MOVE|SET|CHANGE|UPDATE)\s+"
    r"(?:(?:THE|YOUR|MY|OUR)\s+)?"
    r"(?:(?:GOLD|XAUUSD)\s+)?"
    r"(?:SL|STOP\s*LOSS)\s+"
    r"(?:BACK\s+)?(?:TO|AT)?\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

_MOVE_TO_ENTRY = re.compile(
    r"\b(?:MOVE|SET|PUT)\s+"
    r"(?:(?:THE|YOUR|MY|OUR)\s+)?"
    r"(?:(?:GOLD|XAUUSD)\s+)?"
    r"(?:SL|STOP\s*LOSS|STOP|STOPS)\s+"
    r"(?:BACK\s+)?(?:TO|AT)\s+"
    r"(?:ENTRY|BE|BREAKEVEN|BREAK\s+EVEN)\b",
    re.IGNORECASE,
)

_installed = False
_original = None


def _price(value: str) -> str | None:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return format(parsed.normalize(), "f")


def explicit_literal_management(
    raw_text: str,
    *,
    fallback,
) -> Day27ManagementPolicyResult:
    existing = fallback(raw_text)
    if existing.actions:
        return existing

    text = raw_text or ""
    actions: list[dict[str, str | None]] = []
    for match in _NUMERIC_STOP.finditer(text):
        value = _price(match.group(1))
        if value is not None:
            actions.append(
                {"type": "edit_stop_loss", "target": "all", "value": value}
            )
    if not actions and _MOVE_TO_ENTRY.search(text):
        actions.append(
            {"type": "move_to_break_even", "target": "all", "value": None}
        )
    if not actions:
        return existing
    return Day27ManagementPolicyResult(
        tuple(actions),
        "explicit_literal_management_override",
    )


def install_literal_management_overrides() -> None:
    """Install once into both the Day27 module and V1 policy's imported reference."""
    global _installed, _original
    if _installed:
        return

    import app.day27_management_policy as day27
    import app.v1_message_policy as v1

    _original = day27.extract_day27_management_actions

    def wrapped(raw_text: str) -> Day27ManagementPolicyResult:
        return explicit_literal_management(raw_text, fallback=_original)

    day27.extract_day27_management_actions = wrapped
    v1.extract_day27_management_actions = wrapped
    _installed = True


__all__ = [
    "explicit_literal_management",
    "install_literal_management_overrides",
]
