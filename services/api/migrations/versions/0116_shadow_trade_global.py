"""Return TRADE GLOBAL to shadow so it can never reach the broker.

Owner instruction, 2026-09-23. Migration 0114 moved TRADE GLOBAL from `shadow` to
`testing` at 11:03 UTC, which routed its signals to MT5. It opened 19 broker positions
between 11:53 and 13:55 UTC and closed them for about -151 USD. The follow-up that set it
back to shadow (a second 0114 migration) collided with the existing 0114 as a second
Alembic head, failed to build, and never deployed. Since 14:07 UTC the only protection
has been a dispatch gate that matches the provider by display name and fails open.

`status='shadow'` is checked before any other dispatch logic and does not depend on the
display name, so it is the robust control. The name gate stays as a second layer.

Safety: shadow also routes a provider's management and close messages away from the
broker, which would strand a live position. So this only acts when TRADE GLOBAL has no
open/planned/pending position AND no positive net broker volume (bought minus sold) on
any of its broker positions - the second check catches a live trade whose app row is
mislabelled, e.g. `error`. At authoring time both were zero; its two `error` rows are
flat at the broker. If either check ever fails, it records why and leaves the status
alone rather than failing startup and taking trading down.

Revision ID: 0116_shadow_trade_global
Revises: 0115_provider_playbook_guard
Create Date: 2026-09-23
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0116_shadow_trade_global"
down_revision: str | None = "0115_provider_playbook_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRADE_GLOBAL_CHAT_ID = -1003925988158


def _audit(bind, event_type: str, source_id, payload: dict) -> None:
    bind.execute(
        sa.text(
            """
            INSERT INTO audit_events(event_type,entity_type,entity_id,payload)
            VALUES (:event_type,'source',:source_id,CAST(:payload AS jsonb))
            """
        ),
        {"event_type": event_type, "source_id": source_id, "payload": json.dumps(payload)},
    )


def upgrade() -> None:
    bind = op.get_bind()
    source = bind.execute(
        sa.text(
            "SELECT id,status FROM sources WHERE chat_id=:chat_id ORDER BY created_at ASC LIMIT 1"
        ),
        {"chat_id": TRADE_GLOBAL_CHAT_ID},
    ).mappings().one_or_none()
    if source is None or source["status"] == "shadow":
        return

    live_rows = bind.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM positions p
            JOIN signals s ON s.id=p.signal_id
            WHERE s.source_id=:source_id AND p.status IN ('open','planned','pending')
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
                    SELECT p.broker_position_id FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    WHERE s.source_id=:source_id AND p.broker_position_id IS NOT NULL
                )
                GROUP BY d.broker_position_id
            ) x WHERE x.net > 0
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
                "source_title": "TRADE GLOBAL",
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
            "UPDATE sources SET status='shadow',updated_at=now() WHERE id=:id AND status<>'shadow'"
        ),
        {"id": source["id"]},
    )
    _audit(
        bind,
        "telegram.source_status_changed",
        source["id"],
        {
            "source_title": "TRADE GLOBAL",
            "previous_status": source["status"],
            "status": "shadow",
            "actor_role": "owner_instruction_migration",
            "actor_display_name": "Super Signals",
            "paper_execution_enabled": False,
            "reason": "owner_instruction_trade_global_never_live",
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE sources SET status='testing',updated_at=now() "
            "WHERE chat_id=:chat_id AND status='shadow'"
        ),
        {"chat_id": TRADE_GLOBAL_CHAT_ID},
    )
