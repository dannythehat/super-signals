"""Enable only the three audited core providers for the fresh paper run.

Revision ID: 0035_enable_core_paper_providers
Revises: 0034_pause_all_sources_for_audit
Create Date: 2026-08-27

Only the exact audited Telegram chat IDs are enabled in paper/testing mode:
- FXTradingVision: -1001651583302
- canonical fast GTMO: -1001640332422
- TIG's Asia Trades: -1003680830069

The slower GTMO mirror (-1002068685216) and every other provider remain paused or revoked.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035_enable_core_paper_providers"
down_revision: str | None = "0034_pause_all_sources_for_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPROVED_CHAT_IDS = (-1001651583302, -1001640332422, -1003680830069)


def upgrade() -> None:
    op.execute(
        """
        UPDATE sources
        SET status='testing', updated_at=now()
        WHERE chat_id IN (-1001651583302, -1001640332422, -1003680830069)
          AND status='paused'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE sources
        SET status='paused', updated_at=now()
        WHERE chat_id IN (-1001651583302, -1001640332422, -1003680830069)
          AND status='testing'
        """
    )
