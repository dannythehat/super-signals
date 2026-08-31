"""Add auditable fixed-dollar Provider Lab benchmark fields.

Revision ID: 0045_provider_benchmark_usd
Revises: 0044_shadow_quality_r
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_provider_benchmark_usd"
down_revision: str | None = "0044_shadow_quality_r"
branch_labels: str | Sequence[str] | None = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shadow_trades",
        sa.Column("benchmark_model", sa.String(length=40), nullable=False, server_default="fixed_1000_10_per_tp_v1"),
    )
    op.add_column(
        "shadow_trades",
        sa.Column("benchmark_start_balance_usd", sa.Numeric(14, 2), nullable=False, server_default="1000"),
    )
    op.add_column(
        "shadow_trades",
        sa.Column("benchmark_risk_per_leg_usd", sa.Numeric(14, 2), nullable=False, server_default="10"),
    )
    op.add_column(
        "shadow_trades",
        sa.Column("benchmark_pnl_usd", sa.Numeric(14, 2), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_shadow_benchmark_model",
        "shadow_trades",
        "benchmark_model IN ('fixed_1000_10_per_tp_v1')",
    )
    op.create_check_constraint(
        "ck_shadow_benchmark_amounts",
        "shadow_trades",
        "benchmark_start_balance_usd = 1000 AND benchmark_risk_per_leg_usd = 10",
    )


def downgrade() -> None:
    op.drop_constraint("ck_shadow_benchmark_amounts", "shadow_trades", type_="check")
    op.drop_constraint("ck_shadow_benchmark_model", "shadow_trades", type_="check")
    op.drop_column("shadow_trades", "benchmark_pnl_usd")
    op.drop_column("shadow_trades", "benchmark_risk_per_leg_usd")
    op.drop_column("shadow_trades", "benchmark_start_balance_usd")
    op.drop_column("shadow_trades", "benchmark_model")
