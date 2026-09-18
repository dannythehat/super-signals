"""Add AIDY provider-brain provenance and an immutable shadow final-gate ledger.

The reasoning model still has no broker authority. These columns persist the evidence
completeness and AIDY's counterfactual action so we can measure what would have happened
before any future paper/live promotion.

Revision ID: 0098_aidy_final_shadow_gate
Revises: 0097_aidy_reasoning_tool_calls
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0098_aidy_final_shadow_gate"
down_revision: str | None = "0097_aidy_reasoning_tool_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE aidy_reasoning_annotations
          ADD COLUMN provider_context_available boolean NOT NULL DEFAULT false,
          ADD COLUMN provider_profile_version_no integer,
          ADD COLUMN preflight_evidence_calls integer NOT NULL DEFAULT 0
            CHECK (preflight_evidence_calls >= 0),
          ADD COLUMN shadow_action varchar(32),
          ADD COLUMN shadow_risk_multiplier numeric(6,4),
          ADD COLUMN shadow_action_reason text,
          ADD CONSTRAINT ck_aidy_reasoning_shadow_action
            CHECK (
              shadow_action IS NULL OR shadow_action IN (
                'take','reduce','hold','reject','need_more_evidence'
              )
            ),
          ADD CONSTRAINT ck_aidy_reasoning_shadow_risk
            CHECK (
              shadow_risk_multiplier IS NULL OR
              (shadow_risk_multiplier >= 0 AND shadow_risk_multiplier <= 1)
            )
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW aidy_final_gate_shadow_ledger AS
        SELECT
          d.id AS decision_id,
          d.source_id,
          COALESCE(NULLIF(s.source_alias,''),s.chat_title) AS provider,
          d.signal_posted_at,
          d.decision_class AS deterministic_decision_class,
          a.lean,
          a.confidence,
          a.shadow_action,
          a.shadow_risk_multiplier,
          a.shadow_action_reason,
          a.market_context_available,
          a.provider_context_available,
          a.provider_profile_version_no,
          a.tool_calls_made,
          a.preflight_evidence_calls,
          a.created_at AS shadow_decided_at,
          o.resolution,
          o.actual_pnl_usd,
          o.baseline_pnl_usd,
          CASE
            WHEN o.actual_pnl_usd IS NULL OR a.shadow_action IS NULL THEN NULL
            WHEN a.shadow_action='take' THEN o.actual_pnl_usd
            WHEN a.shadow_action='reduce'
              THEN o.actual_pnl_usd * COALESCE(a.shadow_risk_multiplier,0)
            ELSE 0
          END AS shadow_pnl_usd,
          CASE
            WHEN o.actual_pnl_usd IS NULL OR a.shadow_action IS NULL THEN NULL
            WHEN a.shadow_action='take' THEN 0
            WHEN a.shadow_action='reduce'
              THEN (o.actual_pnl_usd * COALESCE(a.shadow_risk_multiplier,0)) - o.actual_pnl_usd
            ELSE -o.actual_pnl_usd
          END AS shadow_delta_vs_taken_usd,
          true AS research_only,
          false AS live_money_execution_allowed
        FROM aidy_decisions d
        JOIN aidy_reasoning_annotations a ON a.decision_id=d.id
        JOIN sources s ON s.id=d.source_id
        LEFT JOIN aidy_decision_outcomes o ON o.decision_id=d.id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS aidy_final_gate_shadow_ledger")
    op.execute(
        """
        ALTER TABLE aidy_reasoning_annotations
          DROP CONSTRAINT IF EXISTS ck_aidy_reasoning_shadow_risk,
          DROP CONSTRAINT IF EXISTS ck_aidy_reasoning_shadow_action,
          DROP COLUMN IF EXISTS shadow_action_reason,
          DROP COLUMN IF EXISTS shadow_risk_multiplier,
          DROP COLUMN IF EXISTS shadow_action,
          DROP COLUMN IF EXISTS preflight_evidence_calls,
          DROP COLUMN IF EXISTS provider_profile_version_no,
          DROP COLUMN IF EXISTS provider_context_available
        """
    )
