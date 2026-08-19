"""Permanent regressions from real provider traffic and broker reconciliation.

Valid directional geometry must stay executable, an earlier deterministic validation
failure must not poison a later corrected provider revision, ambiguous MetaAPI mutation
failures must not be blindly retried, and vanished pending tickets must be resolved from
broker history rather than guessed from active-order absence.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution_dispatch_canonical import CanonicalExecutionDispatcher
from app.mt5_execution_day26 import Day26Mt5ExecutionService
from app.pending_reconciliation_canonical import AccountPendingReconciler


def test_tdc_6861_final_buy_geometry_is_directionally_valid() -> None:
    assert Day26Mt5ExecutionService._directionally_valid(
        side="BUY",
        entry_low=Decimal("4377"),
        entry_high=Decimal("4381"),
        stop_loss=Decimal("4371"),
        take_profits=(
            Decimal("4383.5"),
            Decimal("4386"),
            Decimal("4389"),
            Decimal("4410"),
        ),
    )


def test_united_kings_28803_sell_geometry_is_directionally_valid() -> None:
    assert Day26Mt5ExecutionService._directionally_valid(
        side="SELL",
        entry_low=Decimal("4363"),
        entry_high=Decimal("4373"),
        stop_loss=Decimal("4377"),
        take_profits=(Decimal("4358"), Decimal("4355")),
    )


def test_newer_provider_revision_may_follow_non_ambiguous_validation_failure() -> None:
    prior = {
        "outcome": "blocked",
        "error_code": "strict_directional_validation_failed",
        "source_revision_index": 3,
    }
    assert not CanonicalExecutionDispatcher._prior_failure_blocks_revision(prior, 4)


def test_same_provider_revision_never_replays_after_validation_failure() -> None:
    prior = {
        "outcome": "blocked",
        "error_code": "strict_directional_validation_failed",
        "source_revision_index": 3,
    }
    assert CanonicalExecutionDispatcher._prior_failure_blocks_revision(prior, 3)


@pytest.mark.parametrize(
    "code",
    [
        "metaapi_timeout",
        "metaapi_unreachable",
        "metaapi_temporarily_unavailable",
        "day26_partial_execution_rollback_failed",
        "critical_partial_execution_rollback_failed",
    ],
)
def test_newer_revision_never_blind_retries_ambiguous_broker_mutation(code: str) -> None:
    prior = {
        "outcome": "blocked",
        "error_code": code,
        "source_revision_index": 3,
    }
    assert CanonicalExecutionDispatcher._prior_failure_blocks_revision(prior, 4)


def test_legacy_route_without_revision_evidence_fails_closed() -> None:
    prior = {
        "outcome": "blocked",
        "error_code": "strict_directional_validation_failed",
    }
    assert CanonicalExecutionDispatcher._prior_failure_blocks_revision(prior, 4)


def test_pending_history_cancelled_ticket_is_exact_broker_truth() -> None:
    local = {
        "broker_order_id": "1800350376",
        "broker_client_id": "SS_fixture_E7T4",
        "symbol": "XAUUSD",
        "side": "SELL",
    }
    history = [
        {
            "id": "1800350376",
            "clientId": "SS_fixture_E7T4",
            "symbol": "XAUUSD",
            "type": "ORDER_TYPE_SELL_LIMIT",
            "state": "ORDER_STATE_CANCELED",
        }
    ]
    matched = AccountPendingReconciler._matching_history_order(local, history)
    assert matched is history[0]
    assert matched["state"] == "ORDER_STATE_CANCELED"


def test_pending_history_wrong_client_id_is_not_allowed_to_close_local_truth() -> None:
    local = {
        "broker_order_id": "1800350376",
        "broker_client_id": "SS_expected",
        "symbol": "XAUUSD",
        "side": "SELL",
    }
    history = [
        {
            "id": "1800350376",
            "clientId": "SS_other",
            "symbol": "XAUUSD",
            "type": "ORDER_TYPE_SELL_LIMIT",
            "state": "ORDER_STATE_CANCELED",
        }
    ]
    with pytest.raises(ValueError, match="pending_history_client_id_mismatch"):
        AccountPendingReconciler._matching_history_order(local, history)


def test_missing_pending_history_remains_unresolved_not_assumed_cancelled() -> None:
    local = {
        "broker_order_id": "1800350376",
        "broker_client_id": "SS_expected",
        "symbol": "XAUUSD",
        "side": "SELL",
    }
    assert AccountPendingReconciler._matching_history_order(local, []) is None


def test_canonical_signal_ledger_exposes_real_revision_operation() -> None:
    from app.canonical_signal_ledger import CanonicalSignalLedger

    assert callable(CanonicalSignalLedger.revise)
    assert CanonicalSignalLedger.revise.__name__ == "revise"
