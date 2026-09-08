from datetime import datetime, timedelta, timezone

import pytest

from app.provider_day16_veto_counterfactual import (
    LIVE_VETO_ALLOWED,
    PAPER_VETO_ALLOWED,
    PreregisteredWeakSetupEvidence,
    decide_veto_counterfactual,
    engineering_acceptance_snapshot,
    resolve_counterfactual,
    veto_effectiveness_report,
)


NOW = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)


def weak(**overrides):
    values = dict(
        hypothesis_id="00000000-0000-0000-0000-000000000001",
        preregistered_at=NOW - timedelta(days=2),
        evidence_cutoff=NOW - timedelta(minutes=1),
        cell_oos_n=40,
        complement_oos_n=50,
        shrunken_effect_r=-0.40,
        bh_rejected=True,
        minimum_effect_gate_met=True,
        minimum_oos_gate_met=True,
    )
    values.update(overrides)
    return PreregisteredWeakSetupEvidence(**values)


def decision(evidence):
    return decide_veto_counterfactual(
        signal_id="signal-1",
        source_id="source-1",
        signal_posted_at=NOW,
        decided_at=NOW + timedelta(seconds=2),
        evidence=evidence,
    )


def test_strong_preregistered_negative_evidence_creates_research_reject_only():
    result = decision([weak()])
    assert result.research_action == "reject"
    assert result.executable is False
    assert result.telegram_broadcast_allowed is False
    assert LIVE_VETO_ALLOWED is False
    assert PAPER_VETO_ALLOWED is False


def test_tiny_n_cannot_reject():
    result = decision([weak(cell_oos_n=5)])
    assert result.research_action == "accept"
    assert result.reason == "no_preregistered_negative_oos_gate_met"


def test_non_fdr_or_small_effect_cannot_reject():
    assert decision([weak(bh_rejected=False)]).research_action == "accept"
    assert decision([weak(shrunken_effect_r=-0.10)]).research_action == "accept"


def test_future_preregistration_and_future_evidence_are_rejected():
    with pytest.raises(ValueError, match="future_preregistration"):
        decision([weak(preregistered_at=NOW + timedelta(seconds=1))])
    with pytest.raises(ValueError, match="future_evidence"):
        decision([weak(evidence_cutoff=NOW + timedelta(seconds=1))])


def test_rejected_loss_is_counted_as_avoided_but_winner_as_sacrificed():
    d = decision([weak()])
    loss = resolve_counterfactual(
        decision=d,
        execution_adjusted_baseline_r=-1.2,
        resolved_at=NOW + timedelta(hours=1),
    )
    winner = resolve_counterfactual(
        decision=d,
        execution_adjusted_baseline_r=1.5,
        resolved_at=NOW + timedelta(hours=1),
    )
    assert loss["filtered_execution_adjusted_r"] == 0.0
    assert loss["avoided_loss_r"] == 1.2
    assert loss["sacrificed_winner_r"] == 0.0
    assert winner["avoided_loss_r"] == 0.0
    assert winner["sacrificed_winner_r"] == 1.5


def test_effectiveness_report_never_grants_statistical_or_live_authority():
    d = decision([weak()])
    rows = [
        resolve_counterfactual(
            decision=d,
            execution_adjusted_baseline_r=-1.0,
            resolved_at=NOW + timedelta(hours=1),
        )
    ]
    report = veto_effectiveness_report(rows)
    assert report["engineering_status"] == "GREEN"
    assert report["statistical_status"] == "WAITING-FOR-FORWARD-EVIDENCE"
    assert report["live_veto_allowed"] is False
    assert report["paper_veto_allowed"] is False


def test_engineering_acceptance_snapshot_is_green_but_authority_waits():
    snapshot = engineering_acceptance_snapshot()
    assert snapshot["engineering_harness_green"] is True
    assert snapshot["statistical_authority"] == "WAITING-FOR-FORWARD-EVIDENCE"
    assert snapshot["skipped_trade_telegram_broadcast_allowed"] is False


def test_migration_is_append_only_and_chained_from_0066():
    from pathlib import Path

    migration = Path("services/api/migrations/versions/0067_provider_day16_veto.py").read_text()
    assert 'down_revision: str | None = "0066_shadow_enrollment_audit"' in migration
    assert "provider_day16_append_only" in migration
    assert "BEFORE UPDATE OR DELETE" in migration
    assert "CHECK (NOT telegram_broadcast_allowed)" in migration
    assert "CHECK (NOT live_money_execution_allowed)" in migration
