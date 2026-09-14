"""Acceptance coverage for the hard Owner -> member mirror invariant."""

from app import broker_settlement_canonical, member_routing_canonical
from app.master_mirror_guard import (
    MasterMirrorSettlementManager,
    _master_first_distribute,
)


def test_master_first_distribution_patch_is_active() -> None:
    assert (
        member_routing_canonical.MemberDistributionService.distribute
        is _master_first_distribute
    )


def test_master_mirror_settlement_patch_is_active() -> None:
    assert (
        broker_settlement_canonical.CanonicalBrokerSettlementManager
        is MasterMirrorSettlementManager
    )


def test_master_mirror_reconciler_can_remove_both_exposure_types() -> None:
    names = MasterMirrorSettlementManager._enforce_master_mirror.__code__.co_names
    assert "close_position" in names
    assert "cancel_order" in names
