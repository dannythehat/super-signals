"""Single production source of truth for provider execution and risk policy.

Only rules explicitly approved by the owner belong here. Historical audit rankings,
old migration comments and provider-performance experiments must never alter live trade
eligibility or risk. Production execution imports this module directly; no other module
may invent a provider direction veto or a hidden TP risk ladder.

Owner authority: every enabled provider trade carries exactly 1% planned risk in total.
That one-percent trade budget is divided evenly across the trade's TP/runner broker legs.
Entry sections distribute those target legs; they never multiply the risk budget.
"""

from __future__ import annotations

from decimal import Decimal

from app.risk_sizing_day24 import ApprovedProviderRisk

POLICY_GENERATION = "owner-authority-2026-09-22-v3-one-percent-total-trade"


def _approved(value: str) -> ApprovedProviderRisk:
    return ApprovedProviderRisk(value)


_TOTAL_TRADE_RISK = Decimal("1")
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
    # at its approved three targets; each target now carries the same global 1% risk.
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
    """Return the owner's global 1%-total-trade allocation.

    ``position_count`` is the executed target/runner leg count, not entry_count ×
    target_count. The one-percent trade budget is shared equally across those legs.
    """
    if position_count < 1:
        raise ValueError("provider_position_count_invalid")

    # Disabled provider directions continue to fail closed before broker mutation.
    if not provider_side_enabled(source_name=source_name, side=side):
        return (_DISABLED_PROFILE_RISK,) * position_count

    per_leg = (_TOTAL_TRADE_RISK / Decimal(position_count)).quantize(Decimal("0.0001"))
    profile = [per_leg] * position_count
    # Preserve exactly 1% after decimal quantisation by placing any residual on TP1.
    profile[0] += _TOTAL_TRADE_RISK - sum(profile, Decimal("0"))
    return tuple(profile)
