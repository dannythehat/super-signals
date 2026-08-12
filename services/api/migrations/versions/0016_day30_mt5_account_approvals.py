"""Add one approved live Vantage MT5 login/server per user.

Revision ID: 0016_day30_mt5_account_approvals
Revises: 0015_v1_open_runner
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_day30_mt5_account_approvals"
down_revision: str | None = "0015_v1_open_runner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mt5_account_approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("broker", sa.String(length=40), nullable=False, server_default="vantage"),
        sa.Column("platform", sa.String(length=12), nullable=False, server_default="mt5"),
        sa.Column("account_environment", sa.String(length=12), nullable=False, server_default="live"),
        sa.Column("login", sa.String(length=32), nullable=False),
        sa.Column("server", sa.String(length=160), nullable=False),
        sa.Column(
            "approved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "approved_at",
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
        sa.CheckConstraint("broker = 'vantage'", name="ck_mt5_account_approvals_broker"),
        sa.CheckConstraint("platform = 'mt5'", name="ck_mt5_account_approvals_platform"),
        sa.CheckConstraint("account_environment = 'live'", name="ck_mt5_account_approvals_environment"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_mt5_account_approvals_status"),
        sa.UniqueConstraint("user_id", name="uq_mt5_account_approvals_user"),
    )
    op.create_index(
        "ix_mt5_account_approvals_status",
        "mt5_account_approvals",
        ["status"],
    )
    op.execute("ALTER TABLE mt5_account_approvals ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_mt5_account_approvals_status", table_name="mt5_account_approvals")
    op.drop_table("mt5_account_approvals")
