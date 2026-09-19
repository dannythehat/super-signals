"""Expose AIDY anti-drift grounding health without mutating legacy decisions.

Revision ID: 0104_aidy_grounding_health
Revises: 0103_aidy_gold_state
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0104_aidy_grounding_health"
down_revision: str | None = "0103_aidy_gold_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE VIEW aidy_reasoning_grounding_health AS
        SELECT
            COUNT(*)::bigint AS total_annotations,
            COUNT(*) FILTER (
                WHERE evidence_contract_version='legacy_unvalidated'
            )::bigint AS legacy_unvalidated_rows,
            COUNT(*) FILTER (
                WHERE evidence_contract_version LIKE 'aidy_reasoning_evidence_%'
            )::bigint AS grounded_forward_rows,
            COUNT(*) FILTER (
                WHERE evidence_contract_version LIKE 'aidy_reasoning_evidence_%'
                  AND claim_validation_status='passed'
            )::bigint AS grounded_passed_rows,
            COUNT(*) FILTER (
                WHERE evidence_contract_version LIKE 'aidy_reasoning_evidence_%'
                  AND unsupported_claim_count<>0
            )::bigint AS unsupported_forward_rows,
            COUNT(*) FILTER (
                WHERE evidence_contract_version LIKE 'aidy_reasoning_evidence_%'
                  AND jsonb_array_length(provider_claim_refs)>0
            )::bigint AS provider_claim_rows,
            COUNT(*) FILTER (
                WHERE evidence_contract_version='legacy_unvalidated'
                  AND lower(
                    coalesce(rationale,'') || ' ' ||
                    coalesce(shadow_action_reason,'') || ' ' ||
                    coalesce(key_factors::text,'')
                  ) ~
                  '(histor|track[[:space:]]*record|win[[:space:]]*rate|weaker[[:space:]]+side|stronger[[:space:]]+side|best[[:space:]]+(side|session)|worst[[:space:]]+(side|session)|provider[[:space:]]+(history|performance|record|profile)|their[[:space:]]+(history|record|performance)|((buy|sell).{0,36}(weak|strong|better|worse|outperform|underperform))|((asia|london|new[ _-]?york|overlap|late).{0,40}(weak|strong|better|worse|perform|prefer|favou?r)))'
            )::bigint AS suspicious_legacy_provider_history_rows,
            MAX(created_at) FILTER (
                WHERE evidence_contract_version LIKE 'aidy_reasoning_evidence_%'
            ) AS latest_grounded_at
        FROM aidy_reasoning_annotations
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS aidy_reasoning_grounding_health")
