"""Pause all GTMO Telegram sources from active Super Signals intake.

Revision ID: 0037_pause_gtmo_provider
Revises: 0036_provider_risk_5pct
Create Date: 2026-08-28

Both known GTMO feeds are paused so no new GTMO signals are ingested/executed.
Historical messages, trades and performance records are preserved for audit/reporting.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037_pause_gtmo_provider"
down_revision: str | None = "0036_provider_risk_5pct"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GTMO_CHAT_IDS = (-1001640332422, -1002068685216)


def upgrade() -> None:
    op.execute(
        """
        UPDATE sources
        SET status='paused', updated_at=now()
        WHERE chat_id IN (-1001640332422, -1002068685216)
          AND status <> 'revoked'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE sources
        SET status='testing', updated_at=now()
        WHERE chat_id = -1001640332422
          AND status='paused'
        """
    )
