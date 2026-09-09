"""Single production source of truth for provider execution and risk policy.

Only rules explicitly approved by the owner belong here. Historical audit rankings,
old migration comments and provider-performance experiments must never alter live trade
eligibility or risk. Production execution imports this module directly; no other module
may invent a provider direction veto or a hidden TP risk ladder.
"""

from __future__ import annotations

from decimal import Decimal

from app.risk_sizing_day24 import ApprovedProviderRisk

POLICY_GENERATION = "owner-authority-2026-09-09-v1"


def _approved(value: str) -> ApprovedProviderRisk:
    return ApprovedProviderRisk(value)


# Current owner-approved FXTradingVision rule: BUY and SELL use the same profile.
# TP1 = 5%, TP2 = 5%, TP3 = 1%.
_FX_BUY_PROFILE = (_approved("5"), _approved("5"), _approved("1"))
_FX_SELL_PROFILE = (_approved("5"), _approved("5"), _approved("1"))
_MEDIUM_HEAD = (_approved("1"), _approved("1"))
_DEVELOPING_LEG_RISK = _approved("0.5")
_TIG_ONE_PERCENT_RISK = _approved("1")
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
        # TIG Asia is explicitly BUY + SELL. Historical ranking/audit direction
        # restrictions are not production authority and must never veto either side.
        return True
    if _is_sureshot(source_name=source_name):
        return normalized_side == "SELL"
    if _is_united_kings(source_name=source_name):
        return normalized_side == "SELL"
    return True


def provider_tp_limit(*, source_name: str, side: str) -> int | None:
    # FX is explicitly a three-target profile. TIG keeps every legitimate provider
    # target; no hidden historical target cap is allowed.
    normalized_side = side.strip().upper()
    if _is_fxtradingvision(source_name=source_name) and normalized_side in {"BUY", "SELL"}:
        return 3
    return None


def _head_with_half_percent_tail(position_count: int) -> tuple[Decimal, ...]:
    if position_count < 1:
        raise ValueError("provider_position_count_invalid")
    head = _MEDIUM_HEAD[:position_count]
    extra_count = max(0, position_count - len(_MEDIUM_HEAD))
    return head + ((_DEVELOPING_LEG_RISK,) * extra_count)


def provider_risk_profile(
    *,
    source_name: str,
    side: str,
    position_count: int,
) -> tuple[Decimal, ...] | None:
    if position_count < 1:
        raise ValueError("provider_position_count_invalid")

    # Zero-risk profiles are converted by the execution engine into its canonical
    # sizing failure before any broker mutation, keeping explicitly disabled sides
    # fail closed. No direction can be disabled anywhere else in production code.
    if not provider_side_enabled(source_name=source_name, side=side):
        return (_DISABLED_PROFILE_RISK,) * position_count

    normalized_side = side.strip().upper()
    if _is_fxtradingvision(source_name=source_name):
        profile = _FX_BUY_PROFILE if normalized_side == "BUY" else _FX_SELL_PROFILE
        if position_count > len(profile):
            raise ValueError("fxtradingvision_position_count_invalid")
        return profile[:position_count]

    if is_gtmo_buy(source_name=source_name, side=side):
        return _head_with_half_percent_tail(position_count)

    if _is_tig(source_name=source_name):
        # Current locked rule: selected 1% means every atomic TIG provider
        # position/leg carries 1%, for both BUY and SELL.
        return (_TIG_ONE_PERCENT_RISK,) * position_count

    if _is_sureshot(source_name=source_name) and normalized_side == "SELL":
        return _head_with_half_percent_tail(position_count)

    if _is_united_kings(source_name=source_name) and normalized_side == "SELL":
        return (_DEVELOPING_LEG_RISK,) * position_count

    # Unlisted providers use the user's selected risk unchanged. There is no generic
    # historical TP ladder here; the execution service must preserve the selection.
    return None
