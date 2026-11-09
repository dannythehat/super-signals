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
    # Canonical Telegram must reserve active owner trade references cheaply.
    # Without this partial index it scans thousands of unrelated audit events
    # every publisher poll, contributing to the original feed bottleneck.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_audit_owner_demo_placed_recent
        ON audit_events (created_at DESC, entity_id)
        WHERE entity_type='signal'
          AND event_type='mt5.owner_demo_700207861_execution_success'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_audit_owner_demo_placed_recent")
    op.execute("DROP INDEX IF EXISTS ix_signals_owner_demo_recent")
