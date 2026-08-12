"""Add per-user risk and automation activation state.

Revision ID: 0017_day31_trading_controls
Revises: 0016_day30_mt5_account_approvals
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_day31_trading_controls"
down_revision: str | None = "0016_day30_mt5_account_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_trading_controls",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "risk_percent",
            sa.Numeric(3, 1),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "allow_double_lot",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "trading_status",
            sa.String(length=16),
            nullable=False,
            server_default="stopped",
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "risk_percent IN (0.5, 1.0, 1.5, 2.0)",
            name="ck_user_trading_controls_risk_percent",
        ),
        sa.CheckConstraint(
            "trading_status IN ('stopped', 'active')",
            name="ck_user_trading_controls_status",
        ),
    )
    op.execute("ALTER TABLE user_trading_controls ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("user_trading_controls")
