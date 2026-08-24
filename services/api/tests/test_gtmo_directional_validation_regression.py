from decimal import Decimal

from app.mt5_execution_day26 import Day26Mt5ExecutionService


def test_gtmo_buy_zone_61565_has_valid_directional_geometry() -> None:
    assert Day26Mt5ExecutionService._directionally_valid(
        side="BUY",
        entry_low=Decimal("4630"),
        entry_high=Decimal("4633"),
        stop_loss=Decimal("4627"),
        take_profits=(
            Decimal("4636"),
            Decimal("4638"),
            Decimal("4640"),
        ),
    )


def test_directional_validator_still_rejects_invalid_buy_geometry() -> None:
    assert not Day26Mt5ExecutionService._directionally_valid(
        side="BUY",
        entry_low=Decimal("4630"),
        entry_high=Decimal("4633"),
        stop_loss=Decimal("4631"),
        take_profits=(Decimal("4636"),),
    )
