"""Approved provider-specific risk profiles.

These rules are deliberately exact: provider, direction and TP index must all match.
Provider directions explicitly disabled by the owner fail closed before broker order
submission. Everything else continues through the ordinary member-selected risk policy.
"""

from __future__ import annotations

from decimal import Decimal

_FX_BUY_PROFILE = (Decimal("4"), Decimal("4"), Decimal("2"))
_DISABLED_PROFILE_RISK = Decimal("0")


def _key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _is_fxtradingvision(*, source_name: str) -> bool:
    return _key(source_name).startswith("fxtradingvision")


def _is_gtmo(*, source_name: str) -> bool:
    return _key(source_name).startswith("gtmo")


def is_fxtradingvision_buy(*, source_name: str, side: str) -> bool:
    return _is_fxtradingvision(source_name=source_name) and side.strip().upper() == "BUY"


def provider_side_enabled(*, source_name: str, side: str) -> bool:
    """Return False only for owner-approved disabled provider directions."""
    normalized_side = side.strip().upper()
    if normalized_side != "SELL":
        return True
    return not (
        _is_fxtradingvision(source_name=source_name)
        or _is_gtmo(source_name=source_name)
    )


def provider_tp_limit(*, source_name: str, side: str) -> int | None:
    return 3 if is_fxtradingvision_buy(source_name=source_name, side=side) else None


def provider_risk_profile(
    *,
    source_name: str,
    side: str,
    position_count: int,
) -> tuple[Decimal, ...] | None:
    if position_count < 1:
        raise ValueError("provider_position_count_invalid")

    # The execution engine already converts an invalid risk percentage into the
    # canonical Day26ExecutionError before any broker mutation. Returning a zero-risk
    # sentinel profile therefore blocks both owner-paper and member execution through
    # the normal fail-closed path instead of leaking a generic policy exception.
    if not provider_side_enabled(source_name=source_name, side=side):
        return (_DISABLED_PROFILE_RISK,) * position_count

    if not is_fxtradingvision_buy(source_name=source_name, side=side):
        return None
    if position_count > len(_FX_BUY_PROFILE):
        raise ValueError("fxtradingvision_buy_position_count_invalid")
    return _FX_BUY_PROFILE[:position_count]
