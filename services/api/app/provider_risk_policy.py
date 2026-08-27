"""Approved provider-specific execution/risk policy.

Owner-approved provider rules are exact and fail closed before broker submission.
FXTradingVision and GTMO BUY allocations are explicit. SELL is disabled for both.
"""

from __future__ import annotations

from decimal import Decimal

_FX_BUY_PROFILE = (Decimal("4"), Decimal("4"), Decimal("2"))
_GTMO_BUY_HEAD = (Decimal("4"), Decimal("3"), Decimal("2"))
_GTMO_ADDITIONAL_LEG_RISK = Decimal("0.5")
_DISABLED_PROFILE_RISK = Decimal("0")


def _key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _is_fxtradingvision(*, source_name: str) -> bool:
    return _key(source_name).startswith("fxtradingvision")


def _is_gtmo(*, source_name: str) -> bool:
    return _key(source_name).startswith("gtmo")


def is_fxtradingvision_buy(*, source_name: str, side: str) -> bool:
    return _is_fxtradingvision(source_name=source_name) and side.strip().upper() == "BUY"


def is_gtmo_buy(*, source_name: str, side: str) -> bool:
    return _is_gtmo(source_name=source_name) and side.strip().upper() == "BUY"


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
    # FX BUY is intentionally limited to TP1-TP3. GTMO keeps every provider-supplied
    # additional numeric target/runner; those additional legs receive 0.5% each.
    return 3 if is_fxtradingvision_buy(source_name=source_name, side=side) else None


def _gtmo_profile(position_count: int) -> tuple[Decimal, ...]:
    if position_count < 1:
        raise ValueError("provider_position_count_invalid")
    head = _GTMO_BUY_HEAD[:position_count]
    extra_count = max(0, position_count - len(_GTMO_BUY_HEAD))
    return head + ((_GTMO_ADDITIONAL_LEG_RISK,) * extra_count)


def provider_risk_profile(
    *,
    source_name: str,
    side: str,
    position_count: int,
) -> tuple[Decimal, ...] | None:
    if position_count < 1:
        raise ValueError("provider_position_count_invalid")

    # The execution engine converts zero risk into its canonical risk sizing failure
    # before broker mutation. This is the fail-closed SELL switch for approved providers.
    if not provider_side_enabled(source_name=source_name, side=side):
        return (_DISABLED_PROFILE_RISK,) * position_count

    if is_fxtradingvision_buy(source_name=source_name, side=side):
        if position_count > len(_FX_BUY_PROFILE):
            raise ValueError("fxtradingvision_buy_position_count_invalid")
        return _FX_BUY_PROFILE[:position_count]

    if is_gtmo_buy(source_name=source_name, side=side):
        return _gtmo_profile(position_count)

    return None
