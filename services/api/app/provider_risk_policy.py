"""Approved provider-specific execution/risk policy.

Owner-approved provider rules are exact and fail closed before broker submission.
FXTradingVision, GTMO and TIG allocations are explicit. GTMO SELL is disabled;
FX trades both directions and TIG trades only the approved TP subset for each direction.
"""

from __future__ import annotations

from decimal import Decimal

from app.risk_sizing_day24 import ApprovedProviderRisk


def _approved(value: str) -> ApprovedProviderRisk:
    return ApprovedProviderRisk(value)


_FX_PROFILE = (_approved("5"), _approved("5"), _approved("1"))
_GTMO_BUY_HEAD = (_approved("4"), _approved("3"), _approved("2"))
_GTMO_ADDITIONAL_LEG_RISK = _approved("0.5")
_TIG_BUY_PROFILE = (
    _approved("4"),
    _approved("3"),
    _approved("2"),
    _approved("1"),
)
_TIG_SELL_PROFILE = (_approved("5"), _approved("5"))
_DISABLED_PROFILE_RISK = Decimal("0")


def _key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _is_fxtradingvision(*, source_name: str) -> bool:
    return _key(source_name).startswith("fxtradingvision")


def _is_gtmo(*, source_name: str) -> bool:
    return _key(source_name).startswith("gtmo")


def _is_tig(*, source_name: str) -> bool:
    return _key(source_name).startswith("tigsasiatrades")


def is_fxtradingvision_buy(*, source_name: str, side: str) -> bool:
    return _is_fxtradingvision(source_name=source_name) and side.strip().upper() == "BUY"


def is_gtmo_buy(*, source_name: str, side: str) -> bool:
    return _is_gtmo(source_name=source_name) and side.strip().upper() == "BUY"


def is_tig_buy(*, source_name: str, side: str) -> bool:
    return _is_tig(source_name=source_name) and side.strip().upper() == "BUY"


def is_tig_sell(*, source_name: str, side: str) -> bool:
    return _is_tig(source_name=source_name) and side.strip().upper() == "SELL"


def provider_side_enabled(*, source_name: str, side: str) -> bool:
    """Return False only for owner-approved disabled provider directions."""
    normalized_side = side.strip().upper()
    if normalized_side != "SELL":
        return True
    return not _is_gtmo(source_name=source_name)


def provider_tp_limit(*, source_name: str, side: str) -> int | None:
    # Explicit limits remove all later numeric targets and any open runner before
    # sizing/broker submission. GTMO keeps every supplied additional target/runner.
    normalized_side = side.strip().upper()
    if _is_fxtradingvision(source_name=source_name) and normalized_side in {"BUY", "SELL"}:
        return 3
    if is_tig_buy(source_name=source_name, side=side):
        return 4
    if is_tig_sell(source_name=source_name, side=side):
        return 2
    return None


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
    # before broker mutation. This remains the fail-closed SELL switch for GTMO.
    if not provider_side_enabled(source_name=source_name, side=side):
        return (_DISABLED_PROFILE_RISK,) * position_count

    normalized_side = side.strip().upper()
    if _is_fxtradingvision(source_name=source_name) and normalized_side in {"BUY", "SELL"}:
        if position_count > len(_FX_PROFILE):
            raise ValueError("fxtradingvision_position_count_invalid")
        return _FX_PROFILE[:position_count]

    if is_gtmo_buy(source_name=source_name, side=side):
        return _gtmo_profile(position_count)

    if is_tig_buy(source_name=source_name, side=side):
        if position_count > len(_TIG_BUY_PROFILE):
            raise ValueError("tig_buy_position_count_invalid")
        return _TIG_BUY_PROFILE[:position_count]

    if is_tig_sell(source_name=source_name, side=side):
        if position_count > len(_TIG_SELL_PROFILE):
            raise ValueError("tig_sell_position_count_invalid")
        return _TIG_SELL_PROFILE[:position_count]

    return None
