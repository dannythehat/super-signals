"""Enforce the owner's current profit-only provider policy.

Revision ID: 0121_profit_only_active_sources
Revises: 0120_explicit_shadow_overrides
Create Date: 2026-09-24

For the current provider basket:
* benchmark scoreboard P/L > 0 with resolved evidence => testing/owner execution ON
* P/L <= 0 or no resolved evidence => shadow
* no provider-name or style exception applies
* historical signals are never replayed
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0121_profit_only_active_sources"
down_revision: str | None = "0120_explicit_shadow_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT
                s.id,
                s.status,
                COALESCE(NULLIF(s.chat_title,''),s.source_alias,s.id::text) AS title,
                COALESCE(ps.net_pnl_usd,0) AS pnl,
                COALESCE(ps.trades_resolved,0) AS resolved
            FROM sources s
            LEFT JOIN provider_trade_scoreboard ps ON ps.source_id=s.id
            WHERE s.status IN ('testing','shadow','live')
            ORDER BY s.created_at
            """
        )
    ).mappings().all()

    for row in rows:
        pnl = row["pnl"]
        resolved = int(row["resolved"] or 0)
        wanted = "testing" if pnl is not None and pnl > 0 and resolved > 0 else "shadow"
        previous = str(row["status"])
        if previous == wanted:
            continue

        bind.execute(
            sa.text(
                """
                UPDATE sources
                SET status=:status,updated_at=now()
                WHERE id=:source_id
                """
            ),
            {"status": wanted, "source_id": row["id"]},
        )

        if wanted == "testing":
            bind.execute(
                sa.text(
                    """
                    INSERT INTO provider_execution_probation(
                        source_id,enabled_at,enabled_note,graduated,graduated_at,
                        created_at,updated_at
                    )
                    VALUES(
                        :source_id,now(),:note,false,NULL,now(),now()
                    )
                    ON CONFLICT (source_id) DO NOTHING
                    """
                ),
                {
                    "source_id": row["id"],
                    "note": "0121 profit-only active-source policy: positive provider P/L enables owner execution.",
                },
            )

        bind.execute(
            sa.text(
                """
                INSERT INTO audit_events(event_type,entity_type,entity_id,payload)
                VALUES(
                    'telegram.source_status_changed',
                    'source',
                    :source_id,
                    CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "source_id": row["id"],
                "payload": json.dumps(
                    {
                        "source_title": str(row["title"]),
                        "previous_status": previous,
                        "status": wanted,
                        "paper_execution_enabled": wanted == "testing",
                        "historical_replay_enabled": False,
                        "provider_scoreboard_pnl_usd": float(pnl or 0),
                        "provider_scoreboard_resolved_trades": resolved,
                        "reason": "owner_profit_only_provider_policy",
                    }
                ),
            },
        )


def downgrade() -> None:
    pass
