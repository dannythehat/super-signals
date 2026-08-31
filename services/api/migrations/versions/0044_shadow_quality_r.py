"""Add strategy-independent normalized R to shadow-provider evaluation.

Revision ID: 0044_shadow_quality_r
Revises: 0043_provider_research_profiles
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_shadow_quality_r"
down_revision: str | None = "0043_provider_research_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shadow_trades",
        sa.Column(
            "quality_r_multiple",
            sa.Numeric(14, 6),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "shadow_trades",
        sa.Column(
            "quality_model",
            sa.String(length=32),
            nullable=False,
            server_default="equal_tp_r_v1",
        ),
    )
    op.create_check_constraint(
        "ck_shadow_quality_model",
        "shadow_trades",
        "quality_model IN ('equal_tp_r_v1')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_shadow_quality_model", "shadow_trades", type_="check")
    op.drop_column("shadow_trades", "quality_model")
    op.drop_column("shadow_trades", "quality_r_multiple")
