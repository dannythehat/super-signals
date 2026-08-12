"""Focused Day 31 risk/activation contract tests."""

from __future__ import annotations

from decimal import Decimal

from app.risk_sizing_day24 import BrokerVolumeRules, Day24RiskSizer
from app.routes.trading_controls_day31 import (
    ActivateRequest,
    RiskSettingsRequest,
    TradingControlResponse,
)


def _response(*, risk: str = "1", allow_double: bool = True) -> TradingControlResponse:
    base = Decimal(risk)
    return TradingControlResponse(
        risk_percent=base,
        allow_double_lot=allow_double,
        effective_normal_risk_percent=base,
        effective_double_lot_risk_percent=base * (Decimal("2") if allow_double else Decimal("1")),
        trading_status="stopped",
    )


def test_recommended_preset_and_user_choices_are_locked() -> None:
    view = _response()
    assert view.recommended_risk_percent == Decimal("1")
    assert view.recommended_allow_double_lot is True
    assert view.allowed_risk_percents == (
        Decimal("0.5"),
        Decimal("1"),
        Decimal("1.5"),
        Decimal("2"),
    )
    assert view.effective_normal_risk_percent == Decimal("1")
    assert view.effective_double_lot_risk_percent == Decimal("2")


def test_user_request_surface_is_only_risk_toggle_and_confirmation() -> None:
    assert set(RiskSettingsRequest.model_fields) == {"risk_percent", "allow_double_lot"}
    assert set(ActivateRequest.model_fields) == {"confirmed"}


def test_recommended_double_lot_flows_into_day24_as_two_percent_per_position() -> None:
    sized = Day24RiskSizer.size(
        balance=Decimal("1000"),
        risk_percent=Decimal("1"),
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("99"),
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        take_profit_count=3,
        volume_rules=BrokerVolumeRules.from_values(
            minimum=Decimal("0.01"), maximum=Decimal("100"), step=Decimal("0.01")
        ),
        signal_requests_double_lot=True,
        double_lot_approved=True,
    )
    assert sized.base_risk_percent == Decimal("1")
    assert sized.effective_risk_percent == Decimal("2")
    assert sized.risk_budget_per_position == Decimal("20")
    assert sized.position_count == 3
    assert sized.total_risk_budget == Decimal("60")


def test_turning_double_lot_off_keeps_base_risk() -> None:
    sized = Day24RiskSizer.size(
        balance=Decimal("1000"),
        risk_percent=Decimal("1"),
        signal_entry_price=Decimal("100"),
        signal_stop_loss=Decimal("99"),
        tick_size=Decimal("0.01"),
        tick_value=Decimal("1"),
        take_profit_count=1,
        volume_rules=BrokerVolumeRules.from_values(
            minimum=Decimal("0.01"), maximum=Decimal("100"), step=Decimal("0.01")
        ),
        signal_requests_double_lot=True,
        double_lot_approved=False,
    )
    assert sized.double_lot_applied is False
    assert sized.effective_risk_percent == Decimal("1")
    assert sized.risk_budget_per_position == Decimal("10")
