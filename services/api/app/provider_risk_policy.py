"""Canonical provider direction policy and strict one-percent PER COMPLETE SIGNAL risk.
A complete signal includes all take-profit legs, runners and allocated entry sections.
No provider can increase this risk by requesting extra targets or DOUBLE LOT.
"""

from __future__ import annotations

from decimal import Decimal

from app.risk_sizing_day24 import ApprovedProviderRisk

POLICY_GENERATION = "owner-authority-2026-11-08-v3-one-percent-per-signal"


def _approved(value: str) -> ApprovedProviderRisk:
    return ApprovedProviderRisk(value)


_ONE_PERCENT_PER_SIGNAL = Decimal("1")
_DISABLED_PROFILE_RISK = Decimal("0")


def _key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _is_fxtradingvision(*, source_name: str) -> bool:
    return _key(source_name).startswith("fxtradingvision")


def _is_gtmo(*, source_name: str) -> bool:
    return _key(source_name).startswith("gtmo")


def _is_tig(*, source_name: str) -> bool:
    return _key(source_name).startswith("tigsasiatrades")


def _is_sureshot(*, source_name: str) -> bool:
    return _key(source_name).startswith("sureshot")


def _is_united_kings(*, source_name: str) -> bool:
    return _key(source_name).startswith("unitedkings")


def is_fxtradingvision_buy(*, source_name: str, side: str) -> bool:
    return _is_fxtradingvision(source_name=source_name) and side.strip().upper() == "BUY"


def is_gtmo_buy(*, source_name: str, side: str) -> bool:
    return _is_gtmo(source_name=source_name) and side.strip().upper() == "BUY"


def is_tig_buy(*, source_name: str, side: str) -> bool:
    return _is_tig(source_name=source_name) and side.strip().upper() == "BUY"


def is_tig_sell(*, source_name: str, side: str) -> bool:
    return _is_tig(source_name=source_name) and side.strip().upper() == "SELL"


def provider_side_enabled(*, source_name: str, side: str) -> bool:
    """Return whether the currently approved provider/direction may execute."""
    normalized_side = side.strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        return False
    if _is_gtmo(source_name=source_name):
        return normalized_side == "BUY"
    if _is_tig(source_name=source_name):
        return True
    if _is_sureshot(source_name=source_name):
        return normalized_side == "SELL"
    if _is_united_kings(source_name=source_name):
        return normalized_side == "SELL"
    return True


def provider_tp_limit(*, source_name: str, side: str) -> int | None:
    # Keep existing direction/target eligibility separate from risk. FX remains capped
    # at its approved three targets; each target shares the same one-percent total signal budget.
    normalized_side = side.strip().upper()
    if _is_fxtradingvision(source_name=source_name) and normalized_side in {"BUY", "SELL"}:
        return 3
    return None


def provider_risk_profile(
    *,
    source_name: str,
    side: str,
    position_count: int,
) -> tuple[Decimal, ...] | None:
    """Split one percent of the account across every TP/runner leg.

    A provider with 1, 3 or 4 targets receives the SAME total risk budget.
    Decimal rounding can leave an immaterial amount unused; it never rounds up.
    """
    if position_count < 1:
        raise ValueError("provider_position_count_invalid")

    # Disabled provider directions continue to fail closed before broker mutation.
    if not provider_side_enabled(source_name=source_name, side=side):
        return (_DISABLED_PROFILE_RISK,) * position_count

    per_leg = _ONE_PERCENT_PER_SIGNAL / Decimal(position_count)
    return tuple(_approved(str(per_leg)) for _ in range(position_count))
