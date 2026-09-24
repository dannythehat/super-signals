"""Preserve exact owner-requested shadow overrides.

Revision ID: 0120_explicit_shadow_overrides
Revises: 0119_profit_only_provider_status
Create Date: 2026-09-24

Historical migration retained because production already applied it. A later migration
supersedes the provider-status policy with the owner's current profit-only rule.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0120_explicit_shadow_overrides"
down_revision: str | None = "0119_profit_only_provider_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCALPING_CHAT_ID = -1004469449988


def _set_shadow(bind, source: dict, reason: str) -> None:
    active = bind.execute(
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
    if int(active or 0) > 0:
        return

    previous = str(source["status"])
    bind.execute(
        sa.text("UPDATE sources SET status='shadow',updated_at=now() WHERE id=:source_id"),
        {"source_id": source["id"]},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO audit_events(event_type,entity_type,entity_id,payload)
            VALUES (
                'telegram.source_status_changed','source',:source_id,
                CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "source_id": source["id"],
            "payload": json.dumps(
                {
                    "source_title": str(source["title"]),
                    "previous_status": previous,
                    "status": "shadow",
                    "paper_execution_enabled": False,
                    "historical_replay_enabled": False,
                    "reason": reason,
                }
            ),
        },
    )


def upgrade() -> None:
    bind = op.get_bind()

    scalping = bind.execute(
        sa.text(
            """
            SELECT id,status,COALESCE(NULLIF(chat_title,''),source_alias,'Scalping 📈') AS title
            FROM sources
            WHERE chat_id=:chat_id AND status<>'revoked'
            ORDER BY updated_at DESC,created_at ASC
            LIMIT 1
            """
        ),
        {"chat_id": SCALPING_CHAT_ID},
    ).mappings().one_or_none()
    if scalping is not None:
        _set_shadow(bind, dict(scalping), "explicit_owner_shadow_scalping_group")

    trade_global = bind.execute(
        sa.text(
            """
            SELECT id,status,COALESCE(NULLIF(chat_title,''),source_alias,'TRADE GLOBAL') AS title
            FROM sources
            WHERE COALESCE(chat_title,source_alias)='TRADE GLOBAL'
              AND status<>'revoked'
            ORDER BY updated_at DESC,created_at ASC
            LIMIT 1
            """
        )
    ).mappings().one_or_none()
    if trade_global is not None:
        _set_shadow(bind, dict(trade_global), "explicit_owner_shadow_trade_global")


def downgrade() -> None:
    pass
