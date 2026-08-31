"""Add Provider Lab research profiles and duplicate/style evidence.

Revision ID: 0043_provider_research_profiles
Revises: 0042_no_broker_evidence
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_provider_research_profiles"
down_revision: str | None = "0042_no_broker_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_research_profiles",
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("research_state", sa.String(length=24), nullable=False, server_default="learning"),
        sa.Column("style", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("observed_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signal_like_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("structured_signal_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("management_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("edited_messages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signal_likelihood", sa.Numeric(6, 5), nullable=False, server_default="0"),
        sa.Column("interpretation_readiness", sa.Numeric(6, 5), nullable=False, server_default="0"),
        sa.Column(
            "duplicate_of_source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("duplicate_score", sa.Numeric(6, 5), nullable=True),
        sa.Column(
            "profile_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "research_state IN ('learning','shadow','duplicate_review','qualified','rejected')",
            name="ck_provider_research_state",
        ),
        sa.CheckConstraint(
            "style IN ('scalper','intraday','swing_or_sparse','mixed','unknown')",
            name="ck_provider_research_style",
        ),
        sa.CheckConstraint(
            "observed_messages >= 0 AND signal_like_messages >= 0 "
            "AND structured_signal_messages >= 0 AND management_messages >= 0 "
            "AND edited_messages >= 0",
            name="ck_provider_research_counts_nonnegative",
        ),
        sa.CheckConstraint(
            "signal_likelihood >= 0 AND signal_likelihood <= 1 "
            "AND interpretation_readiness >= 0 AND interpretation_readiness <= 1",
            name="ck_provider_research_scores",
        ),
        sa.CheckConstraint(
            "duplicate_score IS NULL OR (duplicate_score >= 0 AND duplicate_score <= 1)",
            name="ck_provider_research_duplicate_score",
        ),
        sa.CheckConstraint(
            "duplicate_of_source_id IS NULL OR duplicate_of_source_id <> source_id",
            name="ck_provider_research_not_self_duplicate",
        ),
    )
    op.create_index(
        "ix_provider_research_state_style",
        "provider_research_profiles",
        ["research_state", "style"],
    )
    op.create_index(
        "ix_provider_research_duplicate",
        "provider_research_profiles",
        ["duplicate_of_source_id"],
    )


def downgrade() -> None:
    op.drop_table("provider_research_profiles")
