from decimal import Decimal

from app.provider_risk_policy import (
    provider_risk_profile,
    provider_side_enabled,
    provider_tp_limit,
)


def test_tig_asia_buy_and_sell_are_enabled() -> None:
    source = "TIG’s Asia Trades"
    assert provider_side_enabled(source_name=source, side="BUY")
    assert provider_side_enabled(source_name=source, side="SELL")


def test_tig_asia_has_no_hidden_tp_cap() -> None:
    source = "TIG’s Asia Trades"
    assert provider_tp_limit(source_name=source, side="BUY") is None
    assert provider_tp_limit(source_name=source, side="SELL") is None


def test_tig_asia_buy_uses_one_percent_total_trade_risk() -> None:
    source = "TIG’s Asia Trades"
    assert provider_risk_profile(
        source_name=source,
        side="BUY",
        position_count=4,
    ) == (Decimal("0.25"), Decimal("0.25"), Decimal("0.25"), Decimal("0.25"))


def test_tig_asia_sell_uses_one_percent_total_trade_risk() -> None:
    source = "TIG’s Asia Trades"
    assert provider_risk_profile(
        source_name=source,
        side="SELL",
        position_count=4,
    ) == (Decimal("0.25"), Decimal("0.25"), Decimal("0.25"), Decimal("0.25"))
