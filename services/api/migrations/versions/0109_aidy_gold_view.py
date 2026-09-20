"""Persist AIDY's independent Gold view separately from provider-trade action.

Legacy annotations remain valid with NULL Gold-view fields. New v13 Gold-first reasoning
writes a direction, confidence, horizon and explanation before provider alignment is
evaluated. These columns are research evidence only and create no execution authority.

Revision ID: 0109_aidy_gold_view
Revises: 0108_aidy_hist_stress_schema
Create Date: 2026-09-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0109_aidy_gold_view"
down_revision: str | None = "0108_aidy_hist_stress_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE aidy_reasoning_annotations
        ADD COLUMN gold_view_direction varchar(16),
        ADD COLUMN gold_view_confidence numeric,
        ADD COLUMN gold_view_horizon_minutes integer,
        ADD COLUMN gold_view_reason text,
        ADD COLUMN provider_alignment varchar(16)
        """
    )
    op.execute(
        """
        ALTER TABLE aidy_reasoning_annotations
        ADD CONSTRAINT ck_aidy_reason_gold_view_direction
        CHECK (
            gold_view_direction IS NULL OR
            gold_view_direction IN ('bullish','bearish','neutral','unknown')
        ),
        ADD CONSTRAINT ck_aidy_reason_gold_view_confidence
        CHECK (
            gold_view_confidence IS NULL OR
            (gold_view_confidence >= 0 AND gold_view_confidence <= 1)
        ),
        ADD CONSTRAINT ck_aidy_reason_gold_view_horizon
        CHECK (
            gold_view_horizon_minutes IS NULL OR
            (gold_view_horizon_minutes >= 0 AND gold_view_horizon_minutes <= 240)
        ),
        ADD CONSTRAINT ck_aidy_reason_gold_view_unknown_horizon
        CHECK (
            gold_view_direction IS DISTINCT FROM 'unknown' OR
            gold_view_horizon_minutes = 0
        ),
        ADD CONSTRAINT ck_aidy_reason_provider_alignment
        CHECK (
            provider_alignment IS NULL OR
            provider_alignment IN ('aligned','conflicts','unclear')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_aidy_reasoning_gold_view
        ON aidy_reasoning_annotations(gold_view_direction, provider_alignment, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_aidy_reasoning_gold_view")
    op.execute(
        """
        ALTER TABLE aidy_reasoning_annotations
        DROP CONSTRAINT IF EXISTS ck_aidy_reason_provider_alignment,
        DROP CONSTRAINT IF EXISTS ck_aidy_reason_gold_view_unknown_horizon,
        DROP CONSTRAINT IF EXISTS ck_aidy_reason_gold_view_horizon,
        DROP CONSTRAINT IF EXISTS ck_aidy_reason_gold_view_confidence,
        DROP CONSTRAINT IF EXISTS ck_aidy_reason_gold_view_direction,
        DROP COLUMN IF EXISTS provider_alignment,
        DROP COLUMN IF EXISTS gold_view_reason,
        DROP COLUMN IF EXISTS gold_view_horizon_minutes,
        DROP COLUMN IF EXISTS gold_view_confidence,
        DROP COLUMN IF EXISTS gold_view_direction
        """
    )
