"""Pin the faster GTMO Telegram feed and pause its slower mirror.

Revision ID: 0027_gtmo_canonical_feed
Revises: 0026_notification_suppressed
Create Date: 2026-08-20

The two GTMO chats mirror the same provider instructions. Production evidence from
19 August showed the plain-title chat arriving first on all 73 exact matched posts,
with a median lead of roughly six minutes. Trading both chats duplicates exposure and
corrupts provider-performance measurement, so only the faster chat remains Testing.

This migration changes source routing only. Historical messages, trades and performance
records remain immutable forensic truth.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_gtmo_canonical_feed"
down_revision: str | None = "0026_notification_suppressed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FAST_GTMO_CHAT_ID = -1001640332422
_MIRROR_GTMO_CHAT_ID = -1002068685216


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE sources
        SET status = CASE
                WHEN chat_id = {_FAST_GTMO_CHAT_ID} THEN 'testing'
                WHEN chat_id = {_MIRROR_GTMO_CHAT_ID} THEN 'paused'
                ELSE status
            END,
            updated_at = now()
        WHERE chat_id IN ({_FAST_GTMO_CHAT_ID}, {_MIRROR_GTMO_CHAT_ID})
        """
    )


def downgrade() -> None:
    # Restore the exact pre-migration operational state observed on 20 August 2026.
    # Historical trade/performance rows are intentionally untouched in both directions.
    op.execute(
        f"""
        UPDATE sources
        SET status = CASE
                WHEN chat_id = {_FAST_GTMO_CHAT_ID} THEN 'paused'
                WHEN chat_id = {_MIRROR_GTMO_CHAT_ID} THEN 'testing'
                ELSE status
            END,
            updated_at = now()
        WHERE chat_id IN ({_FAST_GTMO_CHAT_ID}, {_MIRROR_GTMO_CHAT_ID})
        """
    )
