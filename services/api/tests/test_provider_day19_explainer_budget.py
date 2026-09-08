from pathlib import Path

from app.provider_day19_explainer_budget import (
    EvidenceState,
    ResourceBudget,
    ResourceUsage,
    close_explanation,
    engineering_acceptance_snapshot,
    evaluate_resource_budget,
    management_explanation,
    placed_trade_explanation,
)


def evidence(n=3, status="WAITING-FOR-FORWARD-EVIDENCE", confidence=0.9):
    return EvidenceState(n, status, confidence)


def test_skipped_trade_never_gets_public_message():
    result = placed_trade_explanation(
        placed=False,
        direction="SELL",
        market_context="event risk",
        evidence=evidence(),
        risk_note="flat size",
    )
    assert result["broadcast_allowed"] is False
    assert result["text"] is None


def test_tiny_n_wording_is_explicitly_non_authoritative():
    result = placed_trade_explanation(
        placed=True,
        direction="BUY",
        market_context="trend aligned",
        evidence=evidence(4),
        risk_note="flat size",
    )
    assert result["broadcast_allowed"] is True
    assert "N=4" in result["text"]
    assert "not statistically validated" in result["text"]
    assert result["private_provider_name_included"] is False


def test_explanation_surface_has_no_provider_name_parameter():
    import inspect

    params = inspect.signature(placed_trade_explanation).parameters
    assert "provider_name" not in params
    assert "source_name" not in params


def test_management_and_close_only_publish_for_placed_trade():
    assert management_explanation(
        trade_was_placed=False,
        action="protect",
        reason="volatility increased",
        evidence=evidence(),
    )["broadcast_allowed"] is False
    assert close_explanation(
        trade_was_placed=False,
        pnl_r=1.0,
        close_reason="target",
        evidence=evidence(),
    )["broadcast_allowed"] is False


def test_soft_and_hard_resource_limits_are_distinct_and_never_touch_execution():
    budget = ResourceBudget(10, 20, 10, 20, 10, 20, 1.0, 2.0)
    soft = evaluate_resource_budget(
        usage=ResourceUsage(d1_reads=12, estimated_cost_usd=0.5), budget=budget
    )
    hard = evaluate_resource_budget(
        usage=ResourceUsage(d1_reads=21, estimated_cost_usd=0.5), budget=budget
    )
    assert soft["status"] == "SOFT_ALERT"
    assert soft["research_enrichment_allowed"] is True
    assert hard["status"] == "HARD_LIMIT"
    assert hard["research_enrichment_allowed"] is False
    assert hard["live_execution_affected"] is False


def test_day19_migration_chains_and_is_append_only():
    text = Path("services/api/migrations/versions/0068_provider_day19_resource_usage.py").read_text()
    assert 'down_revision: str | None = "0067_provider_day16_veto"' in text
    assert "BEFORE UPDATE OR DELETE" in text
    assert "CHECK (NOT live_execution_affected)" in text


def test_engineering_snapshot_green():
    result = engineering_acceptance_snapshot()
    assert result["engineering_harness_green"] is True
    assert result["skipped_trade_broadcast_allowed"] is False
    assert result["private_provider_names_public"] is False
