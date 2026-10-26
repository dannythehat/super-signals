"""Fixed 0.01-lot PU Prime master copying, without percentage-risk vetoes.

Respect original protective SL/TP, broker minimums, exact 0.01 lot and
available margin. Stop-distance percentage is recorded, not used to veto.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.provider_risk_policy import FIXED_PROVIDER_LEG_VOLUME
from app.risk_sizing_day24 import BrokerVolumeRules, Day24RiskSizingError


class MirrorRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MirroredOrderPlan:
    side: str
    symbol: str
    volume: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None
    entry_quote: Decimal
    stop_risk_usd: Decimal
    margin_usd: Decimal


def _d(value: Any) -> Decimal:
    try:
        x = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MirrorRejected("invalid_numeric_value") from exc
    if not x.is_finite():
        raise MirrorRejected("nonfinite_numeric_value")
    return x


def plan_pu_mirror_leg(
    *,
    source: dict[str, Any],
    account: dict[str, Any],
    quote: dict[str, Any],
    specification: dict[str, Any],
    calculated_margin: Any,
    source_signal_legs: int,
    existing_risk_usd: Any = 0,
    max_entry_deviation: Decimal = Decimal("1.50"),
) -> MirroredOrderPlan:
    if source.get("symbol") != "XAUUSD":
        raise MirrorRejected("not_super_signals_gold")
    position_type = source.get("type")
    if position_type not in ("POSITION_TYPE_BUY", "POSITION_TYPE_SELL"):
        raise MirrorRejected("unsupported_source_position_direction")
    side = "BUY" if position_type == "POSITION_TYPE_BUY" else "SELL"
    if quote.get("symbol") != "XAUUSD.s" or specification.get("symbol") != "XAUUSD.s":
        raise MirrorRejected("pu_gold_symbol_mismatch")
    if account.get("tradeAllowed") is not True or account.get("currency") != "USD":
        raise MirrorRejected("pu_account_not_tradable_usd")
    if source_signal_legs < 1 or source_signal_legs > 10:
        raise MirrorRejected("invalid_signal_leg_count")
    stop = _d(source.get("stopLoss"))
    target = _d(source["takeProfit"]) if source.get("takeProfit") else None
    src_entry = _d(source.get("openPrice"))
    equity = _d(account.get("equity"))
    free_margin = _d(account.get("freeMargin"))
    margin = _d(calculated_margin)
    committed = _d(existing_risk_usd)
    bid, ask = _d(quote.get("bid")), _d(quote.get("ask"))
    quote_entry = ask if side == "BUY" else bid
    if equity <= 0 or free_margin <= 0 or margin <= 0 or ask <= 0 or bid <= 0 or ask < bid:
        raise MirrorRejected("pu_funds_or_quote_invalid")
    if abs(quote_entry - src_entry) > max_entry_deviation:
        raise MirrorRejected("copy_slippage_above_limit")
    if stop <= 0 or (side == "BUY" and stop >= quote_entry) or (
        side == "SELL" and stop <= quote_entry
    ):
        raise MirrorRejected("source_stop_not_protective_at_pu_quote")
    if target is not None and (
        (side == "BUY" and target <= quote_entry)
        or (side == "SELL" and target >= quote_entry)
    ):
        raise MirrorRejected("source_target_already_passed")
    # The user explicitly removed arbitrary percentage risk caps and the
    # additional 20% margin buffer. Broker free margin and exact lot size
    # remain mandatory. The original source stop and take profit are preserved.
    if margin > free_margin:
        raise MirrorRejected("pu_margin_insufficient")
    try:
        rules = BrokerVolumeRules.from_values(
            minimum=specification["minVolume"],
            maximum=specification["maxVolume"],
            step=specification["volumeStep"],
        )
    except (Day24RiskSizingError, KeyError, TypeError, InvalidOperation) as exc:
        raise MirrorRejected("pu_broker_spec_invalid") from exc
    volume = FIXED_PROVIDER_LEG_VOLUME
    steps = (volume - rules.minimum) / rules.step
    if (volume < rules.minimum or volume > rules.maximum
            or steps != steps.to_integral_value()):
        raise MirrorRejected("fixed_lot_not_supported_by_broker")
    tick_size = _d(specification.get("tickSize"))
    tick_value = _d(quote.get("lossTickValue"))
    if tick_size <= 0 or tick_value <= 0:
        raise MirrorRejected("pu_broker_tick_value_invalid")
    stop_risk_usd = abs(quote_entry - stop) / tick_size * tick_value * volume
    return MirroredOrderPlan(
        side=side,
        symbol="XAUUSD.s",
        volume=FIXED_PROVIDER_LEG_VOLUME,
        stop_loss=stop,
        take_profit=target,
        entry_quote=quote_entry,
        stop_risk_usd=stop_risk_usd,
        margin_usd=margin,
    )
