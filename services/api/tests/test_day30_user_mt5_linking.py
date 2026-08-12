"""Focused Day 30 user MT5 linking contract tests."""

from __future__ import annotations

import inspect

import pytest

from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.routes.mt5_approvals_day30 import ApproveUserMt5Request
from app.routes.user_mt5_accounts import UserMt5ConnectRequest, UserMt5StatusResponse


def test_normal_user_connect_surface_is_broker_credentials_only() -> None:
    assert set(UserMt5ConnectRequest.model_fields) == {"login", "password", "server"}
    assert set(ApproveUserMt5Request.model_fields) == {"login", "server"}

    connect_parameters = set(inspect.signature(Day30Mt5ConnectionService.connect_user_live).parameters)
    assert connect_parameters == {"self", "user_id", "login", "password", "server"}

    forbidden = {
        field
        for field in UserMt5StatusResponse.model_fields
        if any(
            term in field.casefold()
            for term in ("password", "metaapi", "token", "cipher", "encrypt")
        )
    }
    assert forbidden == set()


def test_normal_users_are_live_vantage_only() -> None:
    assert Day30Mt5ConnectionService.validate_live_vantage_account(
        "12345678", "VantageInternational-Live"
    ) == ("12345678", "VantageInternational-Live")

    with pytest.raises(Mt5ConnectionError) as demo:
        Day30Mt5ConnectionService.validate_live_vantage_account(
            "12345678", "VantageMarkets-Demo"
        )
    assert demo.value.code == "mt5_demo_not_available"

    with pytest.raises(Mt5ConnectionError) as other_broker:
        Day30Mt5ConnectionService.validate_live_vantage_account(
            "12345678", "SomeOtherBroker-Live"
        )
    assert other_broker.value.code == "mt5_vantage_server_required"


def test_login_must_be_an_mt5_account_number() -> None:
    with pytest.raises(Mt5ConnectionError) as invalid:
        Day30Mt5ConnectionService.validate_live_vantage_account(
            "abc-123", "VantageInternational-Live"
        )
    assert invalid.value.code == "mt5_login_invalid"
