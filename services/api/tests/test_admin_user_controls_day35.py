"""Day 35 safety contract for Owner revoke and emergency controls."""

from __future__ import annotations

import inspect

from app.admin_user_controls_day35 import (
    Day35AdminUserControlService,
    Day35EmergencyPreview,
    Day35RevokePreview,
)
from app.trading_controls_day31 import Day31TradingControlService


def test_day31_close_target_query_can_only_load_mapped_open_super_signals_positions() -> None:
    source = inspect.getsource(Day31TradingControlService._load_open_mapped_positions)

    assert "FROM positions" in source
    assert "WHERE user_id=:user_id" in source
    assert "status='open'" in source
    assert "broker_position_id IS NOT NULL" in source


def test_day31_stop_never_iterates_arbitrary_broker_positions_as_close_targets() -> None:
    source = inspect.getsource(Day31TradingControlService.stop_and_close)

    assert "local_positions = self._load_open_mapped_positions(user_id)" in source
    assert "for position_id, broker_position_id in local_positions" in source
    assert "position_id=broker_position_id" in source
    assert 'manual_or_unmapped_positions_touched": False' in source


def test_revoke_preview_contract_explicitly_excludes_manual_unmapped_positions() -> None:
    assert Day35RevokePreview.__dataclass_fields__["manual_or_unmapped_positions_touched"].default is False
    assert Day35RevokePreview.__dataclass_fields__["broker_trade_action_created"].default is False


def test_emergency_preview_contract_is_preview_only_and_excludes_manual_positions() -> None:
    assert Day35EmergencyPreview.__dataclass_fields__["manual_or_unmapped_positions_touched"].default is False
    assert Day35EmergencyPreview.__dataclass_fields__["broker_trade_action_created"].default is False


def test_emergency_blocks_active_invited_user_controls_without_uuid_array_binding() -> None:
    source = inspect.getsource(Day35AdminUserControlService.emergency_stop)

    assert "UPDATE user_trading_controls utc" in source
    assert "AND EXISTS (" in source
    assert "u.id=utc.user_id" in source
    assert "u.status='active'" in source
    assert "r.name='user'" in source
    assert "ANY(:user_ids)" not in source
