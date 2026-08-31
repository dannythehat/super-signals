"""Allow one Paper and one Real MT5 account per user.

Revision ID: 0048_dual_mt5_accounts
Revises: 0047_provider_fairness_v2
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_dual_mt5_accounts"
down_revision: str | None = "0047_provider_fairness_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_mt5_accounts_owner_user", "mt5_accounts", type_="unique")
    op.create_unique_constraint(
        "uq_mt5_accounts_owner_environment",
        "mt5_accounts",
        ["owner_user_id", "account_environment"],
    )

    op.add_column(
        "user_trading_controls",
        sa.Column("active_account_environment", sa.String(length=12), nullable=True),
    )
    op.create_check_constraint(
        "ck_user_trading_controls_active_environment",
        "user_trading_controls",
        "active_account_environment IS NULL OR active_account_environment IN ('demo', 'live')",
    )

    # Every position must retain the broker account that created it once a user can
    # own both Paper and Real accounts. The column is nullable for legacy/non-broker
    # records; all existing broker-backed users currently have one account and are
    # backfilled unambiguously before a second environment can be connected.
    op.add_column(
        "positions",
        sa.Column("mt5_account_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_positions_mt5_account_id",
        "positions",
        "mt5_accounts",
        ["mt5_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_positions_user_mt5_account",
        "positions",
        ["user_id", "mt5_account_id"],
        unique=False,
    )
    op.execute(
        """
        UPDATE positions AS p
        SET mt5_account_id = m.id
        FROM mt5_accounts AS m
        WHERE m.owner_user_id = p.user_id
          AND m.status != 'revoked'
          AND p.mt5_account_id IS NULL
        """
    )

    # Preserve existing behaviour: whichever single account a user already had remains
    # their active account after the schema starts allowing both environments.
    op.execute(
        """
        UPDATE user_trading_controls AS utc
        SET active_account_environment = m.account_environment,
            updated_at = now()
        FROM mt5_accounts AS m
        WHERE m.owner_user_id = utc.user_id
          AND m.status != 'revoked'
          AND utc.active_account_environment IS NULL
        """
    )


def downgrade() -> None:
    # A downgrade is only safe if no user currently has both environments connected.
    op.execute(
        """
        DELETE FROM mt5_accounts AS newer
        USING mt5_accounts AS older
        WHERE newer.owner_user_id = older.owner_user_id
          AND newer.id != older.id
          AND newer.created_at < older.created_at
        """
    )
    op.drop_index("ix_positions_user_mt5_account", table_name="positions")
    op.drop_constraint("fk_positions_mt5_account_id", "positions", type_="foreignkey")
    op.drop_column("positions", "mt5_account_id")
    op.drop_constraint(
        "ck_user_trading_controls_active_environment",
        "user_trading_controls",
        type_="check",
    )
    op.drop_column("user_trading_controls", "active_account_environment")
    op.drop_constraint(
        "uq_mt5_accounts_owner_environment",
        "mt5_accounts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_mt5_accounts_owner_user",
        "mt5_accounts",
        ["owner_user_id"],
    )
