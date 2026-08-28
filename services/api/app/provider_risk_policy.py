"""Approved provider-specific risk profiles.

These rules are deliberately exact: provider, direction and TP index must all match.
Everything else continues through the ordinary member-selected risk policy.
"""

from __future__ import annotations

from decimal import Decimal

_FX_PROFILE = (Decimal("5"), Decimal("5"), Decimal("1"))


def _key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def is_fxtradingvision(*, source_name: str, side: str) -> bool:
    return _key(source_name).startswith("fxtradingvision") and side.strip().upper() in {
        "BUY",
        "SELL",
    }


def is_fxtradingvision_buy(*, source_name: str, side: str) -> bool:
    """Compatibility helper retained for existing callers/tests."""
    return is_fxtradingvision(source_name=source_name, side=side) and side.strip().upper() == "BUY"


def provider_tp_limit(*, source_name: str, side: str) -> int | None:
    return 3 if is_fxtradingvision(source_name=source_name, side=side) else None


def provider_risk_profile(
    *,
    source_name: str,
    side: str,
    position_count: int,
) -> tuple[Decimal, ...] | None:
    if not is_fxtradingvision(source_name=source_name, side=side):
        return None
    if position_count < 1 or position_count > len(_FX_PROFILE):
        raise ValueError("fxtradingvision_position_count_invalid")
    return _FX_PROFILE[:position_count]
