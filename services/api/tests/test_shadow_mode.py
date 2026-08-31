from decimal import Decimal

from app.shadow_trading import _benchmark_pnl_usd, _decimal, _leg_r


def test_shadow_benchmark_uses_equal_money_for_every_tp_leg():
    # Provider Lab fixes each TP leg at $10 risk from a common $1,000 benchmark.
    assert _benchmark_pnl_usd(Decimal("1")) == Decimal("10.00")
    assert _benchmark_pnl_usd(Decimal("2")) == Decimal("20.00")
    assert _benchmark_pnl_usd(Decimal("-1")) == Decimal("-10.00")


def test_shadow_leg_r_uses_provider_stop_distance_not_tp_number():
    assert _leg_r(
        entry=Decimal("4500"),
        exit_price=Decimal("4510"),
        initial_stop=Decimal("4490"),
        side="BUY",
    ) == Decimal("1")
    assert _leg_r(
        entry=Decimal("4500"),
        exit_price=Decimal("4520"),
        initial_stop=Decimal("4490"),
        side="BUY",
    ) == Decimal("2")


def test_shadow_decimal_rejects_non_finite_values():
    assert _decimal("4633.5") == Decimal("4633.5")
    assert _decimal("NaN") is None
    assert _decimal(True) is None
