"""Day 34 normalization for provider-stated pips evidence.

Provider wording is preserved separately in lifecycle JSON/audit evidence. This helper
only converts an unambiguous complete numeric pips token into the numeric database
column so formatting such as ``+35 pips`` cannot break lifecycle processing.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

_PIPS_TOKEN = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?:pips?)?\s*$",
    re.IGNORECASE,
)


def normalize_provider_pips(value: Any) -> Decimal | None:
    """Return a finite Decimal only for an unambiguous complete pips value."""
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, Decimal):
        return value if value.is_finite() else None

    if isinstance(value, (int, float)):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return parsed if parsed.is_finite() else None

    match = _PIPS_TOKEN.fullmatch(str(value))
    if match is None:
        return None
    try:
        parsed = Decimal(match.group(1))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


__all__ = ["normalize_provider_pips"]
