"""Exact standalone Gold/XAUUSD NOW execution profile.

This is deliberately not a generic parser rule. It applies only to the whole-message
forms BUY GOLD NOW, SELL GOLD NOW, GOLD BUY NOW, GOLD SELL NOW and XAUUSD equivalents.
Provider protection remains absent in canonical Signal truth; execution derives the
paper-tested fallback from the fresh broker quote.
"""

from __future__ import annotations

import re
from decimal import Decimal

PROFILE = "bare_gold_now_50_100"
GOLD_PROVIDER_PIP = Decimal("0.1")
TAKE_PROFIT_PIPS = Decimal("50")
STOP_LOSS_PIPS = Decimal("100")
TAKE_PROFIT_DISTANCE = GOLD_PROVIDER_PIP * TAKE_PROFIT_PIPS
STOP_LOSS_DISTANCE = GOLD_PROVIDER_PIP * STOP_LOSS_PIPS

_BARE_NOW = re.compile(
    r"^\s*(?:(BUY|SELL)\s+(?:GOLD|XAUUSD)|(?:GOLD|XAUUSD)\s+(BUY|SELL))\s+NOW\s*[.!🔥✅🚨⚡]*\s*$",
    re.IGNORECASE,
)


def bare_now_side(raw_text: str) -> str | None:
    match = _BARE_NOW.fullmatch(raw_text or "")
    if match is None:
        return None
    return (match.group(1) or match.group(2) or "").upper() or None


__all__ = [
    "GOLD_PROVIDER_PIP",
    "PROFILE",
    "STOP_LOSS_DISTANCE",
    "STOP_LOSS_PIPS",
    "TAKE_PROFIT_DISTANCE",
    "TAKE_PROFIT_PIPS",
    "bare_now_side",
]
