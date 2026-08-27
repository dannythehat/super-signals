"""Day 24 deterministic per-atomic-leg risk sizing.

The parsed Telegram signal supplies the entry and stop loss. This module never
invents, changes or optimizes either value. It calculates one broker-valid volume
from the real broker account balance, the selected risk percentage and broker-reported
symbol/volume rules. Entry count and TP count never scale the balance used for sizing.
No order placement or MetaAPI network request exists in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import TypeAlias

DecimalInput: TypeAlias = Decimal | str | int | float
_ALLOWED_BASE_RISK_PERCENTS = (
    Decimal("0.5"),
    Decimal("1"),
    Decimal("1.5"),
    Decimal("2"),
    Decimal("4"),
)
_ALLOWED_PROFILE_RISK_PERCENTS = _ALLOWED_BASE_RISK_PERCENTS + (Decimal("3"),)
_DOUBLE_LOT_MULTIPLIER = Decimal("2")
_ONE_HUNDRED = Decimal("100")
_ZERO = Decimal("0")


class Day24RiskSizingError(ValueError):
    """A deterministic sizing failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BrokerVolumeRules:
    minimum: Decimal
    maximum: Decimal
    step: Decimal

    @classmethod
    def from_values(
        cls,
        *,
        minimum: DecimalInput,
        maximum: DecimalInput,
        step: DecimalInput,
    ) -> "BrokerVolumeRules":
        rules = cls(
            minimum=_decimal(minimum),
            maximum=_decimal(maximum),
            step=_decimal(step),
        )
        rules.validate()
        return rules

    def validate(self) -> None:
        if self.minimum <= _ZERO or self.maximum <= _ZERO or self.step <= _ZERO:
            raise Day24RiskSizingError("broker_volume_rules_invalid")
        if self.maximum < self.minimum:
            raise Day24RiskSizingError("broker_volume_rules_invalid")


@dataclass(frozen=True, slots=True)
class Day24PositionSize:
    take_profit_number: int
    volume: Decimal
    risk_budget: Decimal
    actual_risk: Decimal


@dataclass(frozen=True, slots=True)
class Day24RiskSizingResult:
    balance: Decimal
    base_risk_percent: Decimal
    effective_risk_percent: Decimal
    signal_requests_double_lot: bool
    double_lot_approved: bool
    double_lot_applied: bool
    signal_entry_price: Decimal
    signal_stop_loss: Decimal
    stop_distance: Decimal
    tick_size: Decimal
    tick_value: Decimal
    loss_per_lot_at_stop: Decimal
    raw_volume: Decimal
    volume: Decimal
    risk_budget_per_position: Decimal
    actual_risk_per_position: Decimal
    position_count: int
    total_risk_budget: Decimal
    total_actual_risk: Decimal
    positions: tuple[Day24PositionSize, ...]


class Day24RiskSizer:
    """Calculate one broker-valid volume for each atomic broker leg."""

    @classmethod
    def size(
        cls,
        *,
        balance: DecimalInput,
        risk_percent: DecimalInput,
        signal_entry_price: DecimalInput,
        signal_stop_loss: DecimalInput,
        tick_size: DecimalInput,
        tick_value: DecimalInput,
        take_profit_count: int,
        volume_rules: BrokerVolumeRules,
        signal_requests_double_lot: bool = False,
        double_lot_approved: bool = False,
        _allow_profile_risk: bool = False,
    ) -> Day24RiskSizingResult:
        balance_value = _decimal(balance)
        base_risk = _decimal(risk_percent)
        entry = _decimal(signal_entry_price)
        stop = _decimal(signal_stop_loss)
        tick_size_value = _decimal(tick_size)
        tick_value_value = _decimal(tick_value)

        cls._validate_inputs(
            balance=balance_value,
            base_risk=base_risk,
            entry=entry,
            stop=stop,
            tick_size=tick_size_value,
            tick_value=tick_value_value,
            take_profit_count=take_profit_count,
            volume_rules=volume_rules,
            allow_profile_risk=_allow_profile_risk,
        )

        double_lot_applied = signal_requests_double_lot and double_lot_approved
        multiplier = _DOUBLE_LOT_MULTIPLIER if double_lot_applied else Decimal("1")
        effective_risk = base_risk * multiplier
        risk_budget = balance_value * effective_risk / _ONE_HUNDRED
        stop_distance = abs(entry - stop)
        ticks_to_stop = stop_distance / tick_size_value
        loss_per_lot = ticks_to_stop * tick_value_value
        if loss_per_lot <= _ZERO:
            raise Day24RiskSizingError("loss_per_lot_invalid")

        raw_volume = risk_budget / loss_per_lot
        broker_minimum_applied = raw_volume < volume_rules.minimum
        volume = cls._round_volume_down(raw_volume, volume_rules)
        actual_risk = volume * loss_per_lot

        # Normal broker-step rounding must never increase risk above the selected
        # per-leg target. The one intentional exception is the broker's hard minimum
        # trade size: if the calculated volume is smaller, use the minimum lot instead
        # of locally refusing an otherwise valid provider leg. Actual risk is reported
        # truthfully; Vantage/MT5 remains the sole funds/margin acceptance authority
        # when the order is submitted.
        if actual_risk > risk_budget and not broker_minimum_applied:
            raise Day24RiskSizingError("risk_budget_exceeded")

        positions = tuple(
            Day24PositionSize(
                take_profit_number=index,
                volume=volume,
                risk_budget=risk_budget,
                actual_risk=actual_risk,
            )
            for index in range(1, take_profit_count + 1)
        )
        position_count = len(positions)

        return Day24RiskSizingResult(
            balance=balance_value,
            base_risk_percent=base_risk,
            effective_risk_percent=effective_risk,
            signal_requests_double_lot=signal_requests_double_lot,
            double_lot_approved=double_lot_approved,
            double_lot_applied=double_lot_applied,
            signal_entry_price=entry,
            signal_stop_loss=stop,
            stop_distance=stop_distance,
            tick_size=tick_size_value,
            tick_value=tick_value_value,
            loss_per_lot_at_stop=loss_per_lot,
            raw_volume=raw_volume,
            volume=volume,
            risk_budget_per_position=risk_budget,
            actual_risk_per_position=actual_risk,
            position_count=position_count,
            total_risk_budget=risk_budget * Decimal(position_count),
            total_actual_risk=actual_risk * Decimal(position_count),
            positions=positions,
        )

    @classmethod
    def size_profile(
        cls,
        *,
        balance: DecimalInput,
        risk_percents: tuple[DecimalInput, ...],
        signal_entry_price: DecimalInput,
        signal_stop_loss: DecimalInput,
        tick_size: DecimalInput,
        tick_value: DecimalInput,
        volume_rules: BrokerVolumeRules,
    ) -> Day24RiskSizingResult:
        """Size atomic TP legs independently for an explicit approved profile."""
        if not risk_percents:
            raise Day24RiskSizingError("risk_profile_invalid")
        sized = tuple(
            cls.size(
                balance=balance,
                risk_percent=risk,
                signal_entry_price=signal_entry_price,
                signal_stop_loss=signal_stop_loss,
                tick_size=tick_size,
                tick_value=tick_value,
                take_profit_count=1,
                volume_rules=volume_rules,
                _allow_profile_risk=True,
            )
            for risk in risk_percents
        )
        positions = tuple(
            Day24PositionSize(
                take_profit_number=index,
                volume=item.volume,
                risk_budget=item.risk_budget_per_position,
                actual_risk=item.actual_risk_per_position,
            )
            for index, item in enumerate(sized, start=1)
        )
        first = sized[0]
        return Day24RiskSizingResult(
            balance=first.balance,
            base_risk_percent=first.base_risk_percent,
            effective_risk_percent=max(item.effective_risk_percent for item in sized),
            signal_requests_double_lot=False,
            double_lot_approved=False,
            double_lot_applied=False,
            signal_entry_price=first.signal_entry_price,
            signal_stop_loss=first.signal_stop_loss,
            stop_distance=first.stop_distance,
            tick_size=first.tick_size,
            tick_value=first.tick_value,
            loss_per_lot_at_stop=first.loss_per_lot_at_stop,
            raw_volume=first.raw_volume,
            volume=first.volume,
            risk_budget_per_position=first.risk_budget_per_position,
            actual_risk_per_position=first.actual_risk_per_position,
            position_count=len(positions),
            total_risk_budget=sum((item.risk_budget for item in positions), _ZERO),
            total_actual_risk=sum((item.actual_risk for item in positions), _ZERO),
            positions=positions,
        )

    @staticmethod
    def _round_volume_down(raw_volume: Decimal, rules: BrokerVolumeRules) -> Decimal:
        rules.validate()
        if raw_volume < rules.minimum:
            return rules.minimum

        capped = min(raw_volume, rules.maximum)
        steps_from_minimum = ((capped - rules.minimum) / rules.step).to_integral_value(
            rounding=ROUND_FLOOR
        )
        volume = rules.minimum + (steps_from_minimum * rules.step)

        if volume > rules.maximum:
            max_steps = ((rules.maximum - rules.minimum) / rules.step).to_integral_value(
                rounding=ROUND_FLOOR
            )
            volume = rules.minimum + (max_steps * rules.step)

        if volume < rules.minimum or volume > capped:
            raise Day24RiskSizingError("broker_volume_rounding_invalid")
        return volume

    @staticmethod
    def _validate_inputs(
        *,
        balance: Decimal,
        base_risk: Decimal,
        entry: Decimal,
        stop: Decimal,
        tick_size: Decimal,
        tick_value: Decimal,
        take_profit_count: int,
        volume_rules: BrokerVolumeRules,
        allow_profile_risk: bool,
    ) -> None:
        if balance <= _ZERO:
            raise Day24RiskSizingError("balance_invalid")
        allowed = _ALLOWED_PROFILE_RISK_PERCENTS if allow_profile_risk else _ALLOWED_BASE_RISK_PERCENTS
        if base_risk not in allowed:
            raise Day24RiskSizingError("risk_percent_invalid")
        if entry <= _ZERO or stop <= _ZERO or entry == stop:
            raise Day24RiskSizingError("signal_entry_stop_invalid")
        if tick_size <= _ZERO:
            raise Day24RiskSizingError("tick_size_invalid")
        if tick_value <= _ZERO:
            raise Day24RiskSizingError("tick_value_invalid")
        if isinstance(take_profit_count, bool) or take_profit_count < 1:
            raise Day24RiskSizingError("take_profit_count_invalid")
        volume_rules.validate()


def _decimal(value: DecimalInput) -> Decimal:
    if isinstance(value, bool):
        raise Day24RiskSizingError("decimal_input_invalid")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise Day24RiskSizingError("decimal_input_invalid") from exc
    if not result.is_finite():
        raise Day24RiskSizingError("decimal_input_invalid")
    return result
