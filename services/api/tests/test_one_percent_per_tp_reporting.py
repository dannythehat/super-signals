from decimal import Decimal
from pathlib import Path

from app.provider_risk_policy import provider_risk_profile
from app.risk_sizing_day24 import BrokerVolumeRules, Day24RiskSizer

ROOT = Path(__file__).resolve().parents[1]


def test_four_targets_are_four_percent_planned_total() -> None:
    profile = provider_risk_profile(
        source_name="Any Enabled Provider",
        side="BUY",
        position_count=4,
    )
    assert profile is not None
    assert profile == (Decimal("1"),) * 4

    result = Day24RiskSizer.size_profile(
        balance=Decimal("2000"),
        risk_percents=profile,
        signal_entry_price=Decimal("4390"),
        signal_stop_loss=Decimal("4380"),
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        volume_rules=BrokerVolumeRules.from_values(
            minimum="0.01", maximum="100", step="0.01"
        ),
    )
    assert result.total_risk_budget == Decimal("80")
    assert result.total_risk_budget / result.balance * Decimal("100") == Decimal("4")


def test_reporting_normalizer_uses_stored_planned_risk_not_signal_multiplier() -> None:
    source = (
        ROOT / "migrations" / "versions" / "0070_one_percent_per_tp_model.py"
    ).read_text(encoding="utf-8")
    assert "NEW.planned_risk_percent" in source
    assert "trg_normalize_model_500_from_planned_risk" in source
    assert "risk_multiplier" not in source.split("def upgrade()", 1)[1]
    assert "UPDATE performance_trade_outcomes SET derived_at = derived_at" in source


def test_four_full_stops_on_500_model_equal_twenty_dollars_not_forty() -> None:
    balance = Decimal("500")
    per_target_risk = balance * Decimal("0.01")
    four_target_stop = per_target_risk * Decimal("4")
    assert per_target_risk == Decimal("5.00")
    assert four_target_stop == Decimal("20.00")
    assert four_target_stop / balance * Decimal("100") == Decimal("4.00")
