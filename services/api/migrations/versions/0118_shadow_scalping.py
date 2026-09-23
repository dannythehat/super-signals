"""Return Scalping to shadow so it cannot execute on the owner paper account.

Revision ID: 0118_shadow_scalping
Revises: 0117_merge_live_heads
Create Date: 2026-09-23
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0118_shadow_scalping"
down_revision: str | None = "0117_merge_live_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCALPING_CHAT_ID = -1004469449988


def _audit(bind, event_type: str, source_id, payload: dict) -> None:
    bind.execute(
        sa.text(
            """
            INSERT INTO audit_events(event_type,entity_type,entity_id,payload)
            VALUES (:event_type,'source',:source_id,CAST(:payload AS jsonb))
            """
        ),
        {
            "event_type": event_type,
            "source_id": source_id,
            "payload": json.dumps(payload),
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    source = bind.execute(
        sa.text(
            "SELECT id,status,source_alias FROM sources "
            "WHERE chat_id=:chat_id ORDER BY created_at ASC LIMIT 1"
        ),
        {"chat_id": SCALPING_CHAT_ID},
    ).mappings().one_or_none()
    if source is None or source["status"] == "shadow":
        return

    live_rows = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM positions p
            JOIN signals s ON s.id=p.signal_id
            WHERE s.source_id=:source_id
              AND p.status IN ('open','planned','pending')
            """
        ),
        {"source_id": source["id"]},
    ).scalar_one()

    net_open_at_broker = bind.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM (
                SELECT d.broker_position_id,
                       SUM(CASE WHEN d.entry_type='DEAL_ENTRY_IN' THEN d.volume ELSE 0 END)
                     - SUM(CASE WHEN d.entry_type IN ('DEAL_ENTRY_OUT','DEAL_ENTRY_OUT_BY')
                                THEN d.volume ELSE 0 END) AS net
                FROM broker_deals d
                WHERE d.broker_position_id IN (
                    SELECT p.broker_position_id
                    FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    WHERE s.source_id=:source_id
                      AND p.broker_position_id IS NOT NULL
                )
                GROUP BY d.broker_position_id
            ) x
            WHERE x.net > 0
            """
        ),
        {"source_id": source["id"]},
    ).scalar_one()

    if live_rows or net_open_at_broker:
        _audit(
            bind,
            "telegram.source_status_change_skipped",
            source["id"],
            {
                "source_title": str(source["source_alias"] or "Scalping"),
                "wanted_status": "shadow",
                "status": source["status"],
                "open_or_pending_positions": int(live_rows),
                "net_open_broker_positions": int(net_open_at_broker),
                "reason": "shadow_would_strand_live_position_close_messages",
            },
        )
        return

    bind.execute(
        sa.text(
            "UPDATE sources SET status='shadow',updated_at=now() "
            "WHERE id=:id AND status<>'shadow'"
        ),
        {"id": source["id"]},
    )
    _audit(
        bind,
        "telegram.source_status_changed",
        source["id"],
        {
            "source_title": str(source["source_alias"] or "Scalping"),
            "previous_status": source["status"],
            "status": "shadow",
            "actor_role": "owner_instruction_migration",
            "actor_display_name": "Super Signals",
            "paper_execution_enabled": False,
            "reason": "owner_instruction_scalper_shadow_only",
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE sources SET status='testing',updated_at=now() "
            "WHERE chat_id=:chat_id AND status='shadow'"
        ),
        {"chat_id": SCALPING_CHAT_ID},
    )
