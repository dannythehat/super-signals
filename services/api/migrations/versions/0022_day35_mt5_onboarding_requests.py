"""Add member-submitted MT5 approval requests.

Revision ID: 0022_day35_mt5_onboarding_requests
Revises: 0021_day34_push_active_since
Create Date: 2026-08-13

A normal member submits only the Vantage MT5 login/account number and exact server
for Owner review. Trading passwords are never stored in this table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_day35_mt5_onboarding_requests"
down_revision: str | None = "0021_day34_push_active_since"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mt5_account_requests",
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
        sa.Column("status", sa.String(length=16), nullable=False, server_default="requested"),
        sa.Column(
            "reviewed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("broker = 'vantage'", name="ck_mt5_account_requests_broker"),
        sa.CheckConstraint("platform = 'mt5'", name="ck_mt5_account_requests_platform"),
        sa.CheckConstraint("account_environment = 'live'", name="ck_mt5_account_requests_environment"),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'cancelled')",
            name="ck_mt5_account_requests_status",
        ),
        sa.UniqueConstraint("user_id", name="uq_mt5_account_requests_user"),
    )
    op.create_index("ix_mt5_account_requests_status", "mt5_account_requests", ["status"])
    op.execute("ALTER TABLE mt5_account_requests ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_mt5_account_requests_status", table_name="mt5_account_requests")
    op.drop_table("mt5_account_requests")
