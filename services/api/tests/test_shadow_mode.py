from decimal import Decimal

from app.shadow_trading import _decimal, _weight


def test_shadow_default_weights_match_owner_default_without_touching_positions():
    assert [_weight(i) for i in range(1, 6)] == [
        Decimal("2"), Decimal("1"), Decimal("0.5"), Decimal("0.5"), Decimal("0.5")
    ]


def test_shadow_decimal_rejects_non_finite_values():
    assert _decimal("4633.5") == Decimal("4633.5")
    assert _decimal("NaN") is None
    assert _decimal(True) is None
