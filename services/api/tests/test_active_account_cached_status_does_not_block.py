"""Regression coverage for transient MetaAPI disconnects on the active account path.

The background connection monitor writes mt5_accounts.status, but that value can lag a
terminal which has already recovered. Trading/management must therefore fail closed on
live MetaAPI reads, not on the cached status column before a broker read is attempted.
"""

from __future__ import annotations

import inspect

from app.active_account_execution import ActiveAccountCanonicalTradingExecutionService
from app.active_account_management import ActiveAccountCanonicalTradingManagementService
from app.mt5_read_service_day23 import Day23Mt5ReadService


def test_execution_does_not_veto_on_cached_connection_status() -> None:
    active_source = inspect.getsource(
        ActiveAccountCanonicalTradingExecutionService._load_active_account
    )
    input_source = inspect.getsource(
        ActiveAccountCanonicalTradingExecutionService._load_inputs
    )

    assert "status!='revoked'" in active_source
    assert "status != 'revoked'" in input_source
    assert 'raise Day26ExecutionError("mt5_account_not_connected")' not in active_source
    assert 'raise Day26ExecutionError("mt5_account_not_connected")' not in input_source


def test_management_does_not_veto_on_cached_connection_status() -> None:
    source = inspect.getsource(ActiveAccountCanonicalTradingManagementService._load_account)

    assert "status != 'revoked'" in source
    assert 'Day27ManagementError("mt5_account_not_connected"' not in source


def test_day23_live_read_is_the_authoritative_connection_check() -> None:
    source = inspect.getsource(Day23Mt5ReadService.read_owner_live_state)

    assert "Do not use the cached mt5_accounts.status" in source
    assert "resolve_account_region" in source
    assert "read_account_information" in source
    assert "read_positions" in source
    assert "read_symbol_price" in source
