"""Approved provider-specific execution/risk policy.

Owner-approved provider rules are exact and fail closed before broker submission.
Risk is allocated per TP leg from the audited provider/direction ladder. Stronger
providers receive more risk on TP1/TP2; TP3+ remains deliberately small. Directions
that failed the audit are blocked before broker mutation.
"""

from __future__ import annotations

from decimal import Decimal

from app.risk_sizing_day24 import ApprovedProviderRisk


def _approved(value: str) -> ApprovedProviderRisk:
    return ApprovedProviderRisk(value)


_FX_BUY_PROFILE = (_approved("2"), _approved("2"), _approved("0.5"))
_FX_SELL_PROFILE = (_approved("1"), _approved("1"), _approved("0.5"))
_MEDIUM_HEAD = (_approved("1"), _approved("1"))
_DEVELOPING_LEG_RISK = _approved("0.5")
# Restore the owner-approved TIG Asia SELL allocation that existed before the
# August 30 direction-ranking change: two funded SELL legs at 5% each.
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
    """Return whether the audited provider/direction is approved for execution."""
    normalized_side = side.strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        return False
    if _is_gtmo(source_name=source_name):
        return normalized_side == "BUY"
    if _is_tig(source_name=source_name):
        # TIG Asia is explicitly approved in both directions. SELL was accidentally
        # excluded by the August 30 ranked-direction policy despite its prior lock.
        return True
    if _is_sureshot(source_name=source_name):
        return normalized_side == "SELL"
    if _is_united_kings(source_name=source_name):
        return normalized_side == "SELL"
    return True


def provider_tp_limit(*, source_name: str, side: str) -> int | None:
    # Explicit limits remove later numeric targets and any open runner before sizing.
    # TIG BUY TP4 was negative in the audited history, so only TP1-TP3 are funded.
    normalized_side = side.strip().upper()
    if _is_fxtradingvision(source_name=source_name) and normalized_side in {"BUY", "SELL"}:
        return 3
    if is_tig_buy(source_name=source_name, side=side):
        return 3
    if is_tig_sell(source_name=source_name, side=side):
        return 2
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
    # sizing failure before any broker mutation, keeping disabled sides fail closed.
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

    if is_tig_buy(source_name=source_name, side=side):
        if position_count > 3:
            raise ValueError("tig_buy_position_count_invalid")
        return _head_with_half_percent_tail(position_count)

    if is_tig_sell(source_name=source_name, side=side):
        if position_count > len(_TIG_SELL_PROFILE):
            raise ValueError("tig_sell_position_count_invalid")
        return _TIG_SELL_PROFILE[:position_count]

    if _is_sureshot(source_name=source_name) and normalized_side == "SELL":
        return _head_with_half_percent_tail(position_count)

    if _is_united_kings(source_name=source_name) and normalized_side == "SELL":
        return (_DEVELOPING_LEG_RISK,) * position_count

    return None
