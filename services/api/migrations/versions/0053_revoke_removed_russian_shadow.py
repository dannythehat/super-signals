"""Revoke the intentionally removed Russian shadow source.

Revision ID: 0053_revoke_removed_russian
Revises: 0052_mt5_dashboard_snapshot
Create Date: 2026-09-01

The owner intentionally left this Telegram group. Keep its historical evidence, but
remove it from every future listener/recovery plan so it cannot consume Telegram calls
or interfere with active provider ingestion.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0053_revoke_removed_russian"
down_revision: str | None = "0052_mt5_dashboard_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAT_ID = -1002635845600
_ALIAS = "Сигналы по золоту XAUUSD 💬"


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE sources
        SET status='revoked', updated_at=now()
        WHERE chat_id={_CHAT_ID}
          AND source_alias='{_ALIAS}'
          AND status<>'revoked'
        """
    )


def downgrade() -> None:
    # This source was intentionally left by the owner. A rollback must not silently
    # re-enable Telegram recovery for a group the reader no longer belongs to.
    pass
