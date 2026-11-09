"""Bounded indexed fresh owner demo feed without scanning canonical signal history.

Revision ID: 0129_owner_feed
Revises: 0128_oct_positive
Create Date: 2026-11-09
"""
from __future__ import annotations

from alembic import op

revision: str = "0129_owner_feed"
down_revision: str | None = "0128_oct_positive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Feed looks back only 100 seconds. This partial index keeps newest
    # accepted market XAUUSD signals cheap even after years of history.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_signals_owner_demo_recent
        ON signals (created_at DESC)
        WHERE parser_status='accepted'
          AND order_type='market'
          AND symbol='XAUUSD'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_signals_owner_demo_recent")
