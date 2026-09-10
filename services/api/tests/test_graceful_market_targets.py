from decimal import Decimal

from app.graceful_market_targets import original_target_risk, remaining_market_targets


def test_sell_keeps_tp2_and_tp3_after_tp1_is_crossed() -> None:
    indexes, targets = remaining_market_targets(
        side="SELL",
        executable=Decimal("4547.39"),
        stop_loss=Decimal("4580"),
        take_profits=(Decimal("4548"), Decimal("4547"), Decimal("4520")),
    )
    assert indexes == (2, 3)
    assert targets == (Decimal("4547"), Decimal("4520"))


def test_buy_keeps_later_targets_after_early_target_is_crossed() -> None:
    indexes, targets = remaining_market_targets(
        side="BUY",
        executable=Decimal("4552.61"),
        stop_loss=Decimal("4520"),
        take_profits=(Decimal("4552"), Decimal("4554"), Decimal("4560")),
    )
    assert indexes == (2, 3)
    assert targets == (Decimal("4554"), Decimal("4560"))


def test_no_remaining_target_fails_closed_shape() -> None:
    indexes, targets = remaining_market_targets(
        side="SELL",
        executable=Decimal("4519"),
        stop_loss=Decimal("4580"),
        take_profits=(Decimal("4548"), Decimal("4547"), Decimal("4520")),
    )
    assert indexes == ()
    assert targets == ()


def test_invalid_stop_side_does_not_recover_trade() -> None:
    indexes, targets = remaining_market_targets(
        side="SELL",
        executable=Decimal("4581"),
        stop_loss=Decimal("4580"),
        take_profits=(Decimal("4548"), Decimal("4547"), Decimal("4520")),
    )
    assert indexes == ()
    assert targets == ()


def test_fx_risk_uses_one_percent_per_original_tp_number_after_compaction() -> None:
    source = "FXTradingVision l Forex & Crypto Signals 🚀"
    for target_index in (1, 2, 3):
        assert original_target_risk(
            source_name=source,
            side="SELL",
            original_position_count=3,
            original_target_index=target_index,
            selected_risk=Decimal("1"),
        ) == Decimal("1")
