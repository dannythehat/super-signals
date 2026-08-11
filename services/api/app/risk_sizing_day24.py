"""Day 24 deterministic per-position risk sizing.

The engine is deliberately broker-agnostic. Callers supply the broker-reported
symbol tick size/value and volume constraints. No order placement or MetaAPI
network request exists in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import TypeAlias

DecimalInput: TypeAlias = Decimal | str | int | float
_ALLOWED_BASE_RISK_PERCENTS = (Decimal("0.5"), Decimal("1"))
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
    double_lot: bool
    entry_price: Decimal
    stop_loss: Decimal
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
    """Calculate one safe broker volume for every TP position in a signal."""

    @classmethod
    def size(
        cls,
        *,
        balance: DecimalInput,
        risk_percent: DecimalInput,
        entry_price: DecimalInput,
        stop_loss: DecimalInput,
        tick_size: DecimalInput,
        tick_value: DecimalInput,
        take_profit_count: int,
        volume_rules: BrokerVolumeRules,
        double_lot: bool = False,
    ) -> Day24RiskSizingResult:
        balance_value = _decimal(balance)
        base_risk = _decimal(risk_percent)
        entry = _decimal(entry_price)
        stop = _decimal(stop_loss)
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
        )

        effective_risk = base_risk * (_DOUBLE_LOT_MULTIPLIER if double_lot else Decimal("1"))
        risk_budget = balance_value * effective_risk / _ONE_HUNDRED
        stop_distance = abs(entry - stop)
        ticks_to_stop = stop_distance / tick_size_value
        loss_per_lot = ticks_to_stop * tick_value_value
        if loss_per_lot <= _ZERO:
            raise Day24RiskSizingError("loss_per_lot_invalid")

        raw_volume = risk_budget / loss_per_lot
        volume = cls._round_volume_down(raw_volume, volume_rules)
        actual_risk = volume * loss_per_lot

        # This invariant is the central Day 24 safety rule. Broker rounding must
        # never increase a position above the instructed per-position risk.
        if actual_risk > risk_budget:
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
            double_lot=double_lot,
            entry_price=entry,
            stop_loss=stop,
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

    @staticmethod
    def _round_volume_down(raw_volume: Decimal, rules: BrokerVolumeRules) -> Decimal:
        rules.validate()
        if raw_volume < rules.minimum:
            raise Day24RiskSizingError("volume_below_broker_minimum")

        capped = min(raw_volume, rules.maximum)
        steps_from_minimum = ((capped - rules.minimum) / rules.step).to_integral_value(
            rounding=ROUND_FLOOR
        )
        volume = rules.minimum + (steps_from_minimum * rules.step)

        # A non-aligned broker maximum is rounded down to the nearest valid
        # volume based on minimum + n*step, never upward.
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
    ) -> None:
        if balance <= _ZERO:
            raise Day24RiskSizingError("balance_invalid")
        if base_risk not in _ALLOWED_BASE_RISK_PERCENTS:
            raise Day24RiskSizingError("risk_percent_invalid")
        if entry <= _ZERO or stop <= _ZERO or entry == stop:
            raise Day24RiskSizingError("entry_stop_invalid")
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
    except Exception as exc:  # Decimal raises multiple numeric conversion errors.
        raise Day24RiskSizingError("decimal_input_invalid") from exc
    if not result.is_finite():
        raise Day24RiskSizingError("decimal_input_invalid")
    return result
