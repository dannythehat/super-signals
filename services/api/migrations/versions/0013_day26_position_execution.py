"""Add Day 26 broker execution mapping fields to positions.

Revision ID: 0013_day26_position_execution
Revises: 0012_mt5_accounts
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_day26_position_execution"
down_revision: str | None = "0012_mt5_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_positions_risk_percent", "positions", type_="check")
    op.create_check_constraint(
        "ck_positions_risk_percent",
        "positions",
        "planned_risk_percent IN (0.5, 1.0, 1.5, 2.0, 3.0, 4.0)",
    )

    op.add_column("positions", sa.Column("volume", sa.Numeric(18, 8), nullable=True))
    op.add_column("positions", sa.Column("stop_loss", sa.Numeric(24, 10), nullable=True))
    op.add_column("positions", sa.Column("broker_order_id", sa.String(120), nullable=True))
    op.add_column("positions", sa.Column("broker_client_id", sa.String(31), nullable=True))
    op.create_index(
        "uq_positions_broker_client_id",
        "positions",
        ["broker_client_id"],
        unique=True,
        postgresql_where=sa.text("broker_client_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_positions_broker_client_id", table_name="positions")
    op.drop_column("positions", "broker_client_id")
    op.drop_column("positions", "broker_order_id")
    op.drop_column("positions", "stop_loss")
    op.drop_column("positions", "volume")

    op.drop_constraint("ck_positions_risk_percent", "positions", type_="check")
    op.create_check_constraint(
        "ck_positions_risk_percent",
        "positions",
        "planned_risk_percent IN (0.5, 1.0, 2.0)",
    )
