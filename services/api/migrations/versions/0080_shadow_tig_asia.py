"""Move TIG's Asia Trades to shadow-only execution.

Revision ID: 0080_shadow_tig_asia
Revises: 0079_fix_member_entitlements
Create Date: 2026-09-14

TIG remains connected for research/shadow tracking, but Shadow status prevents the
canonical dispatcher from sending new provider trades to the owner broker account or
member accounts.
"""

from collections.abc import Sequence
import json

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
        # No TIG source to move. On production this migration already ran against the
        # real row, so this branch only happens where that provider was never ingested
        # -- a fresh database, a new environment, or the integration suite rebuilding
        # the schema from base. Raising there made the whole chain unreproducible from
        # scratch and left every later database test erroring against a half-built
        # schema. A data migration whose target does not exist has nothing to do.
        return

    payload = json.dumps(
        {
            "source_title": "TIG’s Asia Trades",
            "previous_status": str(row["previous_status"]),
            "status": "shadow",
            "actor_role": "system_migration",
            "actor_display_name": "Super Signals migration",
            "monitoring_started": True,
            "live_trading_enabled": False,
            "reason": "owner_disabled_after_september_pnl_review",
        }
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO audit_events(event_type, entity_type, entity_id, payload)
            VALUES (
                'telegram.source_status_changed',
                'source',
                :source_id,
                CAST(:payload AS jsonb)
            )
            """
        ),
        {"source_id": row["id"], "payload": payload},
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
