"""Share logical Telegram sources across authorised reader accounts.

Revision ID: 0004_shared_telegram_sources
Revises: 0003_role_permissions
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_shared_telegram_sources"
down_revision: str | None = "0003_role_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A logical signal source is shared, but every Telegram reader session stays private.
    # This link table records which private readers can legitimately read a source.
    op.create_table(
        "source_reader_access",
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "telegram_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Preserve every currently active Day 9 source as readable through its original
    # reader. Revoked selections intentionally stay detached.
    op.execute(
        """
        INSERT INTO source_reader_access (source_id, telegram_account_id, created_by_user_id)
        SELECT id, telegram_account_id, created_by_user_id
        FROM sources
        WHERE status <> 'revoked'
        ON CONFLICT DO NOTHING
        """
    )

    # Telegram chat IDs identify the same logical group/channel across reader accounts.
    # Only one non-revoked Source may exist for a chat, preventing double ingestion later.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_sources_active_chat_id
        ON sources (chat_id)
        WHERE status <> 'revoked'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_sources_active_chat_id")
    op.drop_table("source_reader_access")
