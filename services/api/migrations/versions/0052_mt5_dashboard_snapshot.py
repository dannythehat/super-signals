"""Persist the last confirmed MT5 dashboard account snapshot.

Revision ID: 0052_mt5_dashboard_snapshot
Revises: 0051_member_super_defaults
Create Date: 2026-09-01

A transient MetaAPI outage must not make Balance, Equity or Free Margin disappear from
an otherwise configured account. Successful broker reads update this snapshot; failed
reads may display it explicitly as last-confirmed data without claiming it is live.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_mt5_dashboard_snapshot"
down_revision: str | None = "0051_member_super_defaults"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mt5_accounts", sa.Column("last_confirmed_currency", sa.String(16), nullable=True))
    op.add_column(
        "mt5_accounts",
        sa.Column("last_confirmed_balance", sa.Numeric(20, 8), nullable=True),
    )
    op.add_column(
        "mt5_accounts",
        sa.Column("last_confirmed_equity", sa.Numeric(20, 8), nullable=True),
    )
    op.add_column(
        "mt5_accounts",
        sa.Column("last_confirmed_margin", sa.Numeric(20, 8), nullable=True),
    )
    op.add_column(
        "mt5_accounts",
        sa.Column("last_confirmed_free_margin", sa.Numeric(20, 8), nullable=True),
    )
    op.add_column(
        "mt5_accounts",
        sa.Column("last_confirmed_trade_allowed", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "mt5_accounts",
        sa.Column("last_confirmed_account_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mt5_accounts", "last_confirmed_account_at")
    op.drop_column("mt5_accounts", "last_confirmed_trade_allowed")
    op.drop_column("mt5_accounts", "last_confirmed_free_margin")
    op.drop_column("mt5_accounts", "last_confirmed_margin")
    op.drop_column("mt5_accounts", "last_confirmed_equity")
    op.drop_column("mt5_accounts", "last_confirmed_balance")
    op.drop_column("mt5_accounts", "last_confirmed_currency")
