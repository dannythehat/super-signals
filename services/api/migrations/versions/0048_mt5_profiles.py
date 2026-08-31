"""Persist one Demo and one Real MT5 profile per Smart Signals user.

Revision ID: 0048_mt5_profiles
Revises: 0047_provider_fairness_v2
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048_mt5_profiles"
down_revision: str | None = "0047_provider_fairness_v2"
branch_labels: str | Sequence[str] | None = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mt5_account_profiles",
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
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "user_id",
            "account_environment",
            name="uq_mt5_profiles_user_environment",
        ),
        sa.UniqueConstraint("metaapi_account_id", name="uq_mt5_profiles_metaapi_account"),
        sa.CheckConstraint("broker='vantage'", name="ck_mt5_profiles_broker"),
        sa.CheckConstraint("platform='mt5'", name="ck_mt5_profiles_platform"),
        sa.CheckConstraint(
            "account_environment IN ('demo','live')",
            name="ck_mt5_profiles_environment",
        ),
        sa.CheckConstraint(
            "status IN ('connecting','connected','disconnected','error','revoked')",
            name="ck_mt5_profiles_status",
        ),
    )
    op.create_index(
        "uq_mt5_profiles_one_active",
        "mt5_account_profiles",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_mt5_profiles_user_environment",
        "mt5_account_profiles",
        ["user_id", "account_environment"],
    )

    # Preserve every currently connected canonical account as the user's first saved
    # account profile. The existing mt5_accounts row remains the single execution slot.
    op.execute(
        """
        INSERT INTO mt5_account_profiles (
            user_id, broker, platform, account_environment, login, server,
            metaapi_account_id, metaapi_token_ciphertext, metaapi_token_fingerprint,
            status, remote_state, remote_connection_status, last_error_code,
            last_checked_at, last_connected_at, is_active, created_at, updated_at
        )
        SELECT
            owner_user_id, broker, platform, account_environment, login, server,
            metaapi_account_id, metaapi_token_ciphertext, metaapi_token_fingerprint,
            status, remote_state, remote_connection_status, last_error_code,
            last_checked_at, last_connected_at, TRUE, created_at, updated_at
        FROM mt5_accounts
        WHERE status <> 'revoked'
        ON CONFLICT (user_id, account_environment) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_mt5_profiles_user_environment", table_name="mt5_account_profiles")
    op.drop_index("uq_mt5_profiles_one_active", table_name="mt5_account_profiles")
    op.drop_table("mt5_account_profiles")
