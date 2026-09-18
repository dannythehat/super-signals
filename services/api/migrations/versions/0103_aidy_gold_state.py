"""Persist optional AIDY Gold-state dossier on immutable signal context attachments.

Revision ID: 0103_aidy_gold_state
Revises: 0102_aidy_evidence_claims
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0103_aidy_gold_state"
down_revision: str | None = "0102_aidy_evidence_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE provider_signal_context_attachments
        ADD COLUMN gold_state_json jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        ALTER TABLE provider_signal_context_attachments
        ADD CONSTRAINT ck_provider_context_gold_state_object
        CHECK (jsonb_typeof(gold_state_json) = 'object')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE provider_signal_context_attachments
        DROP CONSTRAINT IF EXISTS ck_provider_context_gold_state_object,
        DROP COLUMN IF EXISTS gold_state_json
        """
    )
