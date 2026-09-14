"""Move TIG's Asia Trades to shadow-only execution.

Revision ID: 0080_shadow_tig_asia
Revises: 0079_fix_member_entitlements
Create Date: 2026-09-14

TIG remains connected for research/shadow tracking, but Shadow status prevents the
canonical dispatcher from sending new provider trades to the owner broker account or
member accounts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080_shadow_tig_asia"
down_revision: str | None = "0079_fix_member_entitlements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIG_CHAT_ID = -1003680830069


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            """
            WITH target AS (
                SELECT id, status AS previous_status
                FROM sources
                WHERE chat_id=:chat_id
                  AND status <> 'revoked'
                ORDER BY created_at ASC
                LIMIT 1
            ), changed AS (
                UPDATE sources AS s
                SET status='shadow', updated_at=now()
                FROM target AS t
                WHERE s.id=t.id
                RETURNING s.id, t.previous_status
            )
            SELECT id, previous_status FROM changed
            """
        ),
        {"chat_id": _TIG_CHAT_ID},
    ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("tig_asia_source_not_found")

    bind.execute(
        sa.text(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload)
            VALUES (
                'telegram.source_status_changed',
                'source',
                :source_id,
                jsonb_build_object(
                    'source_title', 'TIG’s Asia Trades',
                    'previous_status', :previous_status,
                    'status', 'shadow',
                    'actor_role', 'system_migration',
                    'actor_display_name', 'Super Signals migration',
                    'changed_at', now(),
                    'monitoring_started', true,
                    'live_trading_enabled', false,
                    'reason', 'owner_disabled_after_september_pnl_review'
                )
            )
            """
        ),
        {
            "source_id": row["id"],
            "previous_status": row["previous_status"],
        },
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE sources
            SET status='testing', updated_at=now()
            WHERE chat_id=:chat_id
              AND status='shadow'
            """
        ).bindparams(chat_id=_TIG_CHAT_ID)
    )
