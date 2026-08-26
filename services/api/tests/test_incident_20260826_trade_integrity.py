"""Regressions for the 26 August pending-layer and fill-mapping incident."""

from __future__ import annotations

from app.trading_management_canonical import CanonicalTradingManagementService


def test_risk_free_all_uses_layer_aware_management() -> None:
    assert CanonicalTradingManagementService._needs_critical_management(
        ({"type": "move_to_break_even", "target": "all", "value": None},)
    )


def test_full_close_all_uses_layer_aware_management() -> None:
    assert CanonicalTradingManagementService._needs_critical_management(
        ({"type": "close", "target": "all", "value": None},)
    )


def test_single_tp_close_does_not_broaden_to_full_pending_cancellation() -> None:
    assert not CanonicalTradingManagementService._needs_critical_management(
        ({"type": "close", "target": "TP1", "value": None},)
    )


def test_plain_stop_edit_keeps_existing_management_path() -> None:
    assert not CanonicalTradingManagementService._needs_critical_management(
        ({"type": "edit_stop_loss", "target": "all", "value": "4620"},)
    )
