"""Enable four owner-approved shadow providers for paper execution.

These providers were producing accepted signals but were still status='shadow', which
hard-routes them away from MT5. The owner explicitly approved them for the same paper
execution lane as the other active providers.

This migration changes only source authority for future messages. It does not replay
historical signals or create/cancel/modify any broker order.

Revision ID: 0114_enable_shadow_providers
Revises: 0113_aidy_reasoning_indexes
Create Date: 2026-09-23
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0114_enable_shadow_providers"
down_revision: str | None = "0113_aidy_reasoning_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDERS = (
    (-1001651583302, "FXTradingVision l Forex & Crypto Signals 🚀"),
    (-1003925988158, "TRADE GLOBAL"),
    (-1001640332422, "GTMO VIP 🤴🏽"),
    (-1003298640045, "Isabelle - Queen Of Gold"),
)


def upgrade() -> None:
    bind = op.get_bind()
    for chat_id, title in _PROVIDERS:
        row = bind.execute(
            sa.text(
                """
                WITH target AS (
                    SELECT id, status AS previous_status
                    FROM sources
                    WHERE chat_id=:chat_id
                    ORDER BY created_at ASC
                    LIMIT 1
                ), changed AS (
                    UPDATE sources AS s
                    SET status='testing', updated_at=now()
                    FROM target AS t
                    WHERE s.id=t.id
                      AND s.status='shadow'
                    RETURNING s.id,t.previous_status
                )
                SELECT id,previous_status FROM changed
                """
            ),
            {"chat_id": chat_id},
        ).mappings().one_or_none()

        if row is None:
            continue

        payload = json.dumps(
            {
                "source_title": title,
                "previous_status": row["previous_status"],
                "status": "testing",
                "actor_role": "owner_approved_migration",
                "actor_display_name": "Super Signals",
                "paper_execution_enabled": True,
                "historical_replay_enabled": False,
                "probation_enabled": False,
                "reason": "owner_explicitly_enabled_shadow_provider_for_paper_execution",
            }
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO audit_events(event_type,entity_type,entity_id,payload)
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
    bind = op.get_bind()
    for chat_id, _title in _PROVIDERS:
        bind.execute(
            sa.text(
                """
                UPDATE sources
                SET status='shadow',updated_at=now()
                WHERE chat_id=:chat_id
                  AND status='testing'
                """
            ),
            {"chat_id": chat_id},
        )
