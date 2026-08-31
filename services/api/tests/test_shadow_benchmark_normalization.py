from decimal import Decimal
from pathlib import Path

from app.shadow_trading import _benchmark_pnl_usd, _leg_r


def test_one_r_on_any_tp_leg_is_always_ten_dollars() -> None:
    assert _benchmark_pnl_usd(Decimal("1")) == Decimal("10.00")
    assert _benchmark_pnl_usd(Decimal("-1")) == Decimal("-10.00")
    assert _benchmark_pnl_usd(Decimal("2.5")) == Decimal("25.00")


def test_buy_and_sell_leg_r_are_directionally_symmetric() -> None:
    buy = _leg_r(
        entry=Decimal("4500"),
        exit_price=Decimal("4510"),
        initial_stop=Decimal("4490"),
        side="BUY",
    )
    sell = _leg_r(
        entry=Decimal("4500"),
        exit_price=Decimal("4490"),
        initial_stop=Decimal("4510"),
        side="SELL",
    )
    assert buy == Decimal("1")
    assert sell == Decimal("1")


def test_shadow_research_does_not_apply_house_breakeven_rules() -> None:
    source = Path(__file__).resolve().parents[1] / "app" / "shadow_trading.py"
    text = source.read_text(encoding="utf-8")
    assert "if 1 in hit and 2 in hit" not in text
    assert "if 3 in hit" not in text
    assert "Research follows the provider's current stop exactly" in text


def test_benchmark_migration_locks_same_balance_and_leg_risk() -> None:
    migration = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0045_provider_benchmark_usd.py"
    text = migration.read_text(encoding="utf-8")
    assert 'server_default="1000"' in text
    assert 'server_default="10"' in text
    assert "benchmark_start_balance_usd = 1000" in text
    assert "benchmark_risk_per_leg_usd = 10" in text
