from app.provider_day17_confidence_sizing import HeatCaps
from app.provider_day18_combined_book import (
    BookLeg,
    ResearchCandidate,
    aggregate_cluster_research,
    book_state,
    engineering_acceptance_snapshot,
    horizons_compatible,
    normalize_margin_mode,
    research_book_decision,
    research_heat_allocation,
)


def _candidate(provider, cluster, direction, edge, low=5, high=30, eligible=True):
    return ResearchCandidate(provider, cluster, direction, edge, low, high, eligible)


def test_margin_mode_normalization_supports_mt5_variants():
    assert normalize_margin_mode("ACCOUNT_MARGIN_MODE_RETAIL_HEDGING") == "hedging"
    assert normalize_margin_mode("RETAIL_HEDGING") == "hedging"
    assert normalize_margin_mode("ACCOUNT_MARGIN_MODE_RETAIL_NETTING") == "netting"
    assert normalize_margin_mode("ACCOUNT_MARGIN_MODE_EXCHANGE") == "netting"
    assert normalize_margin_mode(None) == "unknown"


def test_netting_mode_collapses_opposing_positions_and_blocks_literal_hedge():
    state = book_state(
        [
            BookLeg("p1", "c1", "BUY", 0.010, 5, 30),
            BookLeg("p2", "c2", "SELL", 0.004, 5, 30),
        ],
        account_mode="netting",
    )
    assert state["broker_equivalent_positions"] == 1
    assert state["netting_collapsed_direction"] == "BUY"
    assert state["literal_hedge_supported"] is False
    assert state["provider_attribution_preserved_at_broker_position_level"] is False
    assert state["production_portfolio_authority"] is False


def test_hedging_mode_preserves_provider_attribution():
    state = book_state(
        [
            BookLeg("p1", "c1", "BUY", 0.010, 5, 30),
            BookLeg("p2", "c2", "SELL", 0.004, 5, 30),
        ],
        account_mode="hedging",
    )
    assert state["broker_equivalent_positions"] == 2
    assert state["literal_hedge_supported"] is True
    assert state["provider_attribution_preserved_at_broker_position_level"] is True


def test_both_branch_requires_proven_hedging_and_compatible_horizon():
    candidates = [
        _candidate("p1", "c1", "BUY", 0.5, 5, 30),
        _candidate("p2", "c2", "SELL", 0.4, 10, 25),
    ]
    assert research_book_decision(candidates=candidates, account_mode="hedging")["choice"] == "both"
    assert research_book_decision(candidates=candidates, account_mode="netting")["choice"] != "both"
    unknown = research_book_decision(candidates=candidates, account_mode="unknown")
    assert unknown["choice"] == "none"
    assert unknown["both_branch_allowed"] is False


def test_incompatible_horizons_block_both_even_in_hedging_mode():
    candidates = [
        _candidate("p1", "c1", "BUY", 0.7, 1, 10),
        _candidate("p2", "c2", "SELL", 0.2, 60, 240),
    ]
    result = research_book_decision(candidates=candidates, account_mode="hedging")
    assert result["choice"] == "buy_only"
    assert result["both_branch_allowed"] is False


def test_cluster_aggregation_downweights_relay_copies():
    selected = aggregate_cluster_research(
        [
            _candidate("relay-a", "same-upstream", "BUY", 0.3),
            _candidate("relay-b", "same-upstream", "BUY", 0.5),
            _candidate("independent", "other", "BUY", 0.2),
        ]
    )
    assert len(selected) == 2
    assert {item.provider_id for item in selected} == {"relay-b", "independent"}


def test_conflicting_netting_evidence_can_reduce_instead_of_fake_hedge():
    result = research_book_decision(
        candidates=[
            _candidate("p1", "c1", "BUY", 0.50),
            _candidate("p2", "c2", "SELL", 0.45),
        ],
        account_mode="netting",
    )
    assert result["choice"] == "reduced"
    assert result["both_branch_allowed"] is False


def test_horizon_overlap_is_interval_based():
    assert horizons_compatible(5, 30, 20, 60) is True
    assert horizons_compatible(5, 10, 11, 20) is False


def test_research_heat_allocator_keeps_gross_and_cluster_caps():
    result = research_heat_allocation(
        open_legs=[BookLeg("p1", "relay", "BUY", 0.010, 5, 30)],
        candidate=BookLeg("p2", "relay", "SELL", 0.010, 5, 30),
        caps=HeatCaps(net_cap_fraction=0.02, gross_cap_fraction=0.03, cluster_cap_fraction=0.015),
    )
    assert result["research_only"] is True
    assert result["executable"] is False
    assert result["allowed_risk_fraction"] == 0.005
    assert "provider_cluster_heat_cap" in result["limiting_reasons"]


def test_engineering_snapshot_green_without_production_authority():
    snapshot = engineering_acceptance_snapshot()
    assert snapshot["engineering_harness_green"] is True
    assert snapshot["production_portfolio_authority"] is False
    assert snapshot["account_mode_required"] is True
