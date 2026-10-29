"""Owner 29 October account-balance risk flag regression tests."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.mt5_execution_day26 import Day26Mt5ExecutionService, Day26ExecutionError


def _size(balance, flag=None, risk=Decimal("1")):
    svc = object.__new__(Day26Mt5ExecutionService)
    sig = SimpleNamespace(
        stop_loss=Decimal("2499"), position_count=3,
        signal_requests_double_lot=True,
    )
    mode = {"SUPER_SIGNALS_OWNER_RISK_MODE": flag} if flag else {}
    with patch.dict("os.environ", mode, clear=False):
        if not flag:
            with patch.dict("os.environ", {"SUPER_SIGNALS_OWNER_RISK_MODE": "fixed"}):
                return svc._size_signal(
                    signal=sig, execution_entry=Decimal("2500"),
                    balance=balance, price_loss_tick_value=1.0,
                    specification={"minVolume": 0.01, "maxVolume": 100,
                                   "volumeStep": 0.01, "tickSize": 0.01},
                    risk_percent=risk, double_lot_approved=True,
                )
        return svc._size_signal(
            signal=sig, execution_entry=Decimal("2500"),
            balance=balance, price_loss_tick_value=1.0,
            specification={"minVolume": 0.01, "maxVolume": 100,
                           "volumeStep": 0.01, "tickSize": 0.01},
            risk_percent=risk, double_lot_approved=True,
        )


def test_flag_off_preserves_exact_fixed_lot():
    assert _size(10000).volume == Decimal("0.01")


def test_flag_on_balance_1000_is_ten_dollar_per_tp():
    sized = _size(1000, "one-percent-per-tp")
    assert sized.volume == Decimal("0.10")
    assert sized.actual_risk_per_position == Decimal("10")
    assert sized.total_actual_risk == Decimal("30")


def test_flag_on_balance_10000_is_hundred_dollar_per_tp():
    sized = _size(10000, "one-percent-per-tp")
    assert sized.volume == Decimal("1.0")
    assert sized.actual_risk_per_position == Decimal("100")
    assert sized.total_actual_risk == Decimal("300")
    assert sized.double_lot_applied is False


def test_flag_on_does_not_exceed_one_percent_by_forcing_minimum_lot():
    with pytest.raises(Day26ExecutionError) as ex:
        _size(50, "one-percent-per-tp")
    assert ex.value.code == "broker_minimum_exceeds_risk_budget"


def test_flag_on_preserves_disabled_direction_block():
    with pytest.raises(Day26ExecutionError) as ex:
        _size(10000, "one-percent-per-tp", risk=Decimal("0"))
    assert ex.value.code == "provider_direction_disabled_or_risk_invalid"
