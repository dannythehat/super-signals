"""Enable FXTradingVision on the owner demo/paper execution path.

Revision ID: 0114_fxtradingvision_demo_on
Revises: 0113_aidy_reasoning_indexes
Create Date: 2026-09-23

The owner MT5 account is demo. Setting this source to testing makes its accepted BUY and
SELL signals execute on the owner demo/reference account through the same canonical route
as the other enabled providers. Active probation continues to exclude member LIVE
accounts until explicit graduation.
"""

from __future__ import annotations

from alembic import op

revision = "0114_fxtradingvision_demo_on"
down_revision = "0113_aidy_reasoning_indexes"
branch_labels = None
depends_on = None

_CHAT_ID = -1001651583302
_NOTE = (
    "0114 FXTradingVision enabled for owner demo/paper forward execution on both BUY and SELL; "
    "member LIVE remains excluded until explicit graduation."
)


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO provider_execution_probation (
            source_id, enabled_at, enabled_note, graduated, graduated_at, created_at, updated_at
        )
        SELECT id, now(), '{_NOTE}', false, NULL, now(), now()
        FROM sources
        WHERE chat_id = {_CHAT_ID}
          AND status <> 'revoked'
        ON CONFLICT (source_id) DO UPDATE
        SET enabled_note = EXCLUDED.enabled_note,
            updated_at = now()
        """
    )
    op.execute(
        f"""
        UPDATE sources
        SET status = 'testing', updated_at = now()
        WHERE chat_id = {_CHAT_ID}
          AND status <> 'revoked'
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE sources
        SET status = 'shadow', updated_at = now()
        WHERE chat_id = {_CHAT_ID}
          AND status = 'testing'
        """
    )
    op.execute(
        f"""
        DELETE FROM provider_execution_probation
        WHERE source_id IN (SELECT id FROM sources WHERE chat_id = {_CHAT_ID})
          AND enabled_note = '{_NOTE}'
          AND graduated = false
        """
    )
