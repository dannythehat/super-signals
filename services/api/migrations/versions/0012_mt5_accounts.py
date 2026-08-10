"""Add secure MetaAPI-backed MT5 account records.

Revision ID: 0012_mt5_accounts
Revises: 0011_signal_lifecycle_events
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_mt5_accounts"
down_revision: str | None = "0011_signal_lifecycle_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mt5_accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("broker", sa.String(length=40), nullable=False, server_default="vantage"),
        sa.Column("platform", sa.String(length=12), nullable=False, server_default="mt5"),
        sa.Column("account_environment", sa.String(length=12), nullable=False),
        sa.Column("login", sa.String(length=32), nullable=False),
        sa.Column("server", sa.String(length=160), nullable=False),
        sa.Column("metaapi_account_id", sa.String(length=96), nullable=False),
        sa.Column("metaapi_token_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("metaapi_token_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="connecting"),
        sa.Column("remote_state", sa.String(length=32), nullable=True),
        sa.Column("remote_connection_status", sa.String(length=40), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("broker = 'vantage'", name="ck_mt5_accounts_broker"),
        sa.CheckConstraint("platform = 'mt5'", name="ck_mt5_accounts_platform"),
        sa.CheckConstraint(
            "account_environment IN ('demo', 'live')",
            name="ck_mt5_accounts_environment",
        ),
        sa.CheckConstraint(
            "status IN ('connecting', 'connected', 'disconnected', 'error', 'revoked')",
            name="ck_mt5_accounts_status",
        ),
        sa.UniqueConstraint("owner_user_id", name="uq_mt5_accounts_owner_user"),
        sa.UniqueConstraint("metaapi_account_id", name="uq_mt5_accounts_metaapi_account"),
    )
    op.create_index("ix_mt5_accounts_status", "mt5_accounts", ["status"])
    op.execute("ALTER TABLE mt5_accounts ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("ix_mt5_accounts_status", table_name="mt5_accounts")
    op.drop_table("mt5_accounts")
