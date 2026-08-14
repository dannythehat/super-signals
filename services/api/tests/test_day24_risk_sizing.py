from decimal import Decimal

import pytest

from app.risk_sizing_day24 import (
    BrokerVolumeRules,
    Day24RiskSizer,
    Day24RiskSizingError,
)


def rules(*, minimum: str = "0.01", maximum: str = "100", step: str = "0.01") -> BrokerVolumeRules:
    return BrokerVolumeRules.from_values(minimum=minimum, maximum=maximum, step=step)


@pytest.mark.parametrize(
    ("risk_percent", "expected_budget", "expected_volume"),
    [
        ("0.5", Decimal("5"), Decimal("0.05")),
        ("1", Decimal("10"), Decimal("0.10")),
        ("1.5", Decimal("15"), Decimal("0.15")),
        ("2", Decimal("20"), Decimal("0.20")),
    ],
)
def test_all_user_risk_options_match_hand_calculation(
    risk_percent: str,
    expected_budget: Decimal,
    expected_volume: Decimal,
) -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent=risk_percent,
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=1,
        volume_rules=rules(),
    )

    assert result.risk_budget_per_position == expected_budget
    assert result.loss_per_lot_at_stop == Decimal("100")
    assert result.volume == expected_volume
    assert result.actual_risk_per_position == expected_budget


def test_recommended_preset_contract_is_one_percent_with_double_lot_approved() -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent="1",
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=1,
        volume_rules=rules(),
        signal_requests_double_lot=True,
        double_lot_approved=True,
    )

    assert result.base_risk_percent == Decimal("1")
    assert result.double_lot_approved is True
    assert result.double_lot_applied is True
    assert result.effective_risk_percent == Decimal("2")


def test_one_position_is_created_per_take_profit_with_full_per_position_risk() -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent="1",
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=3,
        volume_rules=rules(),
    )

    assert result.position_count == 3
    assert [item.take_profit_number for item in result.positions] == [1, 2, 3]
    assert all(item.volume == Decimal("0.10") for item in result.positions)
    assert all(item.risk_budget == Decimal("10") for item in result.positions)
    assert result.total_risk_budget == Decimal("30")
    assert result.total_actual_risk == Decimal("30.00")


@pytest.mark.parametrize(
    ("base_risk", "expected_effective_risk", "expected_volume"),
    [
        ("0.5", Decimal("1.0"), Decimal("0.10")),
        ("1", Decimal("2"), Decimal("0.20")),
        ("1.5", Decimal("3.0"), Decimal("0.30")),
        ("2", Decimal("4"), Decimal("0.40")),
    ],
)
def test_double_lot_applies_only_when_signal_requests_it_and_user_approves(
    base_risk: str,
    expected_effective_risk: Decimal,
    expected_volume: Decimal,
) -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent=base_risk,
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=2,
        volume_rules=rules(),
        signal_requests_double_lot=True,
        double_lot_approved=True,
    )

    assert result.double_lot_applied is True
    assert result.effective_risk_percent == expected_effective_risk
    assert result.volume == expected_volume


def test_double_lot_signal_stays_at_normal_risk_when_user_has_turned_it_off() -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent="1",
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=3,
        volume_rules=rules(),
        signal_requests_double_lot=True,
        double_lot_approved=False,
    )

    assert result.signal_requests_double_lot is True
    assert result.double_lot_approved is False
    assert result.double_lot_applied is False
    assert result.effective_risk_percent == Decimal("1")
    assert result.volume == Decimal("0.10")


def test_user_approval_does_not_double_a_normal_signal() -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent="1",
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=1,
        volume_rules=rules(),
        signal_requests_double_lot=False,
        double_lot_approved=True,
    )

    assert result.double_lot_applied is False
    assert result.effective_risk_percent == Decimal("1")
    assert result.volume == Decimal("0.10")


def test_broker_volume_step_rounds_down_and_never_exceeds_risk() -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent="1",
        signal_entry_price="4000",
        signal_stop_loss="4003",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=1,
        volume_rules=rules(),
    )

    assert result.raw_volume > Decimal("0.03")
    assert result.raw_volume < Decimal("0.04")
    assert result.volume == Decimal("0.03")
    assert result.actual_risk_per_position == Decimal("9.00")
    assert result.actual_risk_per_position <= result.risk_budget_per_position


def test_broker_minimum_is_used_when_target_volume_is_smaller() -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent="0.5",
        signal_entry_price="4000",
        signal_stop_loss="4010",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=1,
        volume_rules=rules(minimum="0.01", step="0.01"),
    )

    assert result.raw_volume == Decimal("0.005")
    assert result.volume == Decimal("0.01")
    assert result.risk_budget_per_position == Decimal("5")
    assert result.actual_risk_per_position == Decimal("10.00")


def test_live_small_account_shape_uses_broker_minimum_instead_of_blocking() -> None:
    result = Day24RiskSizer.size(
        balance="1025.35",
        risk_percent="1",
        signal_entry_price="4348.68",
        signal_stop_loss="4325",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=3,
        volume_rules=rules(minimum="0.01", step="0.01"),
        signal_requests_double_lot=True,
        double_lot_approved=True,
    )

    assert result.effective_risk_percent == Decimal("2")
    assert result.raw_volume < Decimal("0.01")
    assert result.volume == Decimal("0.01")
    assert result.position_count == 3
    assert result.actual_risk_per_position == Decimal("23.68")


def test_broker_maximum_caps_volume_without_exceeding_risk() -> None:
    result = Day24RiskSizer.size(
        balance="1000000",
        risk_percent="1",
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=1,
        volume_rules=rules(minimum="0.01", maximum="2", step="0.01"),
    )

    assert result.raw_volume == Decimal("100")
    assert result.volume == Decimal("2.00")
    assert result.actual_risk_per_position == Decimal("200.00")
    assert result.actual_risk_per_position <= result.risk_budget_per_position


def test_non_zero_minimum_and_step_are_respected() -> None:
    result = Day24RiskSizer.size(
        balance="1000",
        risk_percent="1",
        signal_entry_price="4000",
        signal_stop_loss="4001",
        tick_size="0.01",
        tick_value="1",
        take_profit_count=1,
        volume_rules=rules(minimum="0.05", maximum="1", step="0.02"),
    )

    assert result.raw_volume == Decimal("0.1")
    assert result.volume == Decimal("0.09")
    assert result.actual_risk_per_position == Decimal("9.00")


@pytest.mark.parametrize("risk_percent", ["0.25", "0.75", "2.5", "3"])
def test_only_locked_user_risk_options_are_accepted(risk_percent: str) -> None:
    with pytest.raises(Day24RiskSizingError, match="risk_percent_invalid"):
        Day24RiskSizer.size(
            balance="1000",
            risk_percent=risk_percent,
            signal_entry_price="4000",
            signal_stop_loss="4001",
            tick_size="0.01",
            tick_value="1",
            take_profit_count=1,
            volume_rules=rules(),
        )


def test_signal_entry_and_stop_must_differ() -> None:
    with pytest.raises(Day24RiskSizingError, match="signal_entry_stop_invalid"):
        Day24RiskSizer.size(
            balance="1000",
            risk_percent="1",
            signal_entry_price="4000",
            signal_stop_loss="4000",
            tick_size="0.01",
            tick_value="1",
            take_profit_count=1,
            volume_rules=rules(),
        )
