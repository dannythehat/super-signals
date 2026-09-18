"""Ground AIDY provider-history reasoning in immutable evidence references.

New forward reasoning rows persist the exact provider evidence snapshot the model was
allowed to use plus the validated claim IDs it actually relied on. Legacy annotations
remain intact and are explicitly labelled legacy_unvalidated rather than rewritten.

Revision ID: 0102_aidy_evidence_claims
Revises: 0101_aidy_no_pit_terminal
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0102_aidy_evidence_claims"
down_revision: str | None = "0101_aidy_no_pit_terminal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE aidy_reasoning_annotations
            ADD COLUMN evidence_contract_version varchar(64)
                NOT NULL DEFAULT 'legacy_unvalidated',
            ADD COLUMN provider_evidence_snapshot jsonb
                NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN provider_claim_refs jsonb
                NOT NULL DEFAULT '[]'::jsonb,
            ADD COLUMN claim_validation_status varchar(32)
                NOT NULL DEFAULT 'legacy_unvalidated',
            ADD COLUMN unsupported_claim_count integer
                NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_reasoning_annotations
            ADD CONSTRAINT ck_aidy_reasoning_provider_evidence_array
                CHECK (jsonb_typeof(provider_evidence_snapshot) = 'array'),
            ADD CONSTRAINT ck_aidy_reasoning_provider_claim_refs_array
                CHECK (jsonb_typeof(provider_claim_refs) = 'array'),
            ADD CONSTRAINT ck_aidy_reasoning_claim_validation_status
                CHECK (claim_validation_status IN ('legacy_unvalidated','passed')),
            ADD CONSTRAINT ck_aidy_reasoning_unsupported_claim_count
                CHECK (unsupported_claim_count = 0)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_aidy_reasoning_claim_validation
        ON aidy_reasoning_annotations(claim_validation_status, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_aidy_reasoning_claim_validation")
    op.execute(
        """
        ALTER TABLE aidy_reasoning_annotations
            DROP CONSTRAINT IF EXISTS ck_aidy_reasoning_unsupported_claim_count,
            DROP CONSTRAINT IF EXISTS ck_aidy_reasoning_claim_validation_status,
            DROP CONSTRAINT IF EXISTS ck_aidy_reasoning_provider_claim_refs_array,
            DROP CONSTRAINT IF EXISTS ck_aidy_reasoning_provider_evidence_array,
            DROP COLUMN IF EXISTS unsupported_claim_count,
            DROP COLUMN IF EXISTS claim_validation_status,
            DROP COLUMN IF EXISTS provider_claim_refs,
            DROP COLUMN IF EXISTS provider_evidence_snapshot,
            DROP COLUMN IF EXISTS evidence_contract_version
        """
    )
