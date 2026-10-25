"""Strict read-only PU Prime entry planner for accepted GOLD provider signals.

No order, bridge-queue, or database mutation lives here. Stop-loss exposure is checked using the PU broker's own equity, never Vantage.
Every planned TP has exactly 0.01 lots, and no PU live trade is enabled.
This planner is NOT sufficient to make PU copy trading operational.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

from app.provider_risk_policy import (
    FIXED_PROVIDER_LEG_VOLUME,
    FIXED_PROVIDER_MAX_STOP_RISK_PERCENT,
    FIXED_PROVIDER_MAX_SIGNAL_RISK_PERCENT,
    provider_risk_profile,
    provider_tp_limit,
)
from app.risk_sizing_day24 import BrokerVolumeRules, Day24RiskSizer, Day24RiskSizingError


class PuPlanBlocked(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PuPlannedLeg:
    tp_index: int
    volume: Decimal
    take_profit: Decimal | None
    stop_loss: Decimal
    risk_usd: Decimal


@dataclass(frozen=True, slots=True)
class PuEntryPlan:
    signal_id: str
    side: str
    symbol: str
    entry_price: Decimal
    total_risk_usd: Decimal
    legs: tuple[PuPlannedLeg, ...]
    automatic_trading_enabled: bool = False


def plan_pu_entry(
    *,
    signal_id: str,
    symbol: str,
    side: str,
    order_type: str,
    parser_status: str,
    source_status: str,
    source_name: str,
    source_posted_at: datetime,
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    take_profits: tuple[Any, ...],
    has_open_runner: bool,
    account: dict[str, Any],
    price: dict[str, Any],
    specification: dict[str, Any],
    now: datetime | None = None,
    max_signal_risk_percent: Decimal = Decimal("10"),
) -> PuEntryPlan:
    now = now or datetime.now(timezone.utc)
    if source_posted_at.tzinfo is None or now.tzinfo is None:
        raise PuPlanBlocked("unverified_signal_timestamp")
    if now < source_posted_at or now - source_posted_at > timedelta(seconds=120):
        raise PuPlanBlocked("signal_no_longer_fresh")
    if parser_status != "accepted" or source_status not in {"testing", "live"}:
        raise PuPlanBlocked("signal_not_approved_for_live")
    if symbol.strip().upper() != "XAUUSD" or order_type.lower() != "market":
        raise PuPlanBlocked("unsupported_gold_order")
    side = side.strip().upper()
    if side not in {"BUY", "SELL"}:
        raise PuPlanBlocked("invalid_side")
    if account.get("tradeAllowed") is not True:
        raise PuPlanBlocked("broker_trading_disabled")
    try:
        equity = Decimal(str(account["equity"]))
        bid = Decimal(str(price["bid"]))
        ask = Decimal(str(price["ask"]))
        entry_min = Decimal(str(entry_low))
        entry_max = Decimal(str(entry_high))
        stop = Decimal(str(stop_loss))
        raw_targets = tuple(Decimal(str(tp)) for tp in take_profits)
        tick_size = Decimal(str(specification["tickSize"]))
        loss_tick_value = Decimal(str(price["lossTickValue"]))
        rules = BrokerVolumeRules.from_values(
            minimum=specification["minVolume"],
            maximum=specification["maxVolume"],
            step=specification["volumeStep"],
        )
    except (KeyError, TypeError, ValueError, Day24RiskSizingError) as exc:
        raise PuPlanBlocked("broker_or_signal_numeric_invalid") from exc
    if any(not x.is_finite() for x in (equity, bid, ask, entry_min, entry_max, stop, *raw_targets)):
        raise PuPlanBlocked("nonfinite_numeric_value")
    if equity <= 0 or bid <= 0 or ask <= 0 or ask < bid:
        raise PuPlanBlocked("broker_equity_or_quote_invalid")
    if entry_min <= 0 or entry_max < entry_min:
        raise PuPlanBlocked("signal_entry_invalid")

    executable = ask if side == "BUY" else bid
    # Fail closed for provider stops that are already invalid.
    if (side == "BUY" and stop >= executable) or (side == "SELL" and stop <= executable):
        raise PuPlanBlocked("stop_loss_already_crossed")

    tp_limit = provider_tp_limit(source_name=source_name, side=side)
    if tp_limit is not None:
        raw_targets = raw_targets[:tp_limit]
        has_open_runner = False
    if any((b <= a if side == "BUY" else b >= a)
           for a, b in zip(raw_targets, raw_targets[1:])):
        raise PuPlanBlocked("provider_targets_out_of_order")
    if any((tp <= stop if side == "BUY" else tp >= stop) for tp in raw_targets):
        raise PuPlanBlocked("target_direction_invalid")

    initial_target_count = len(raw_targets) + (1 if has_open_runner else 0)
    if initial_target_count == 0 or initial_target_count > 10:
        raise PuPlanBlocked("invalid_provider_target_count")
    profile = provider_risk_profile(
        source_name=source_name, side=side, position_count=initial_target_count,
    )
    assert profile is not None
    if any(risk <= 0 for risk in profile):
        raise PuPlanBlocked("provider_direction_disabled")

    # Preserve the original TP index when an early TP is already behind market.
    viable = [
        (idx, target)
        for idx, target in enumerate(raw_targets, start=1)
        if (target > executable if side == "BUY" else target < executable)
    ]
    if has_open_runner:
        viable.append((initial_target_count, None))
    if not viable:
        raise PuPlanBlocked("all_targets_already_passed")

    legs: list[PuPlannedLeg] = []
    for idx, target in viable:
        # 25 October owner authority: every PU source TP uses exactly 0.01
        # lots, never 1%-of-equity-derived volume or broker rounding.
        # Check the actual stop-loss cash exposure per leg and for the basket.
        try:
            sized = Day24RiskSizer.size_fixed_volume(
                balance=equity,
                signal_entry_price=executable,
                signal_stop_loss=stop,
                tick_size=tick_size,
                tick_value=loss_tick_value,
                take_profit_count=len(viable),
                volume_rules=rules,
                fixed_volume=FIXED_PROVIDER_LEG_VOLUME,
                max_stop_risk_percent=FIXED_PROVIDER_MAX_STOP_RISK_PERCENT,
                max_signal_risk_percent=FIXED_PROVIDER_MAX_SIGNAL_RISK_PERCENT,
            )
        except Day24RiskSizingError as exc:
            raise PuPlanBlocked(exc.code) from exc
        legs.append(PuPlannedLeg(
            tp_index=idx,
            volume=sized.volume,
            take_profit=target,
            stop_loss=stop,
            risk_usd=sized.actual_risk_per_position,
        ))

    total = sum((leg.risk_usd for leg in legs), Decimal(0))
    if total > equity * max_signal_risk_percent / Decimal(100):
        raise PuPlanBlocked("total_signal_risk_cap_exceeded")
    return PuEntryPlan(
        signal_id=str(signal_id),
        side=side,
        symbol="XAUUSD.s",
        entry_price=executable,
        total_risk_usd=total,
        legs=tuple(legs),
    )
