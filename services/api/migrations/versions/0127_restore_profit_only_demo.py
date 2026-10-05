"""Restore the owner's profit-only demo execution basket.

Revision ID: 0127_profit_only_demo
Revises: 0126_local_mt5_bridge
Create Date: 2026-10-05

Only providers with positive resolved scoreboard P/L may execute on the owner
demo account. Paused and revoked sources remain untouched, and no historical
signals are replayed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0127_profit_only_demo"
down_revision: str | None = "0126_local_mt5_bridge"
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
                COALESCE(NULLIF(s.chat_title, ''), s.source_alias, s.id::text) AS title,
                COALESCE(ps.net_pnl_usd, 0) AS pnl,
                COALESCE(ps.trades_resolved, 0) AS resolved
            FROM sources AS s
            LEFT JOIN provider_trade_scoreboard AS ps ON ps.source_id = s.id
            WHERE s.status NOT IN ('paused', 'revoked')
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
                SET status = :status, updated_at = now()
                WHERE id = :source_id
                """
            ),
            {"status": wanted, "source_id": row["id"]},
        )

        if wanted == "testing":
            bind.execute(
                sa.text(
                    """
                    INSERT INTO provider_execution_probation(
                        source_id, enabled_at, enabled_note, graduated, graduated_at,
                        created_at, updated_at
                    )
                    VALUES(
                        :source_id, now(), :note, false, NULL, now(), now()
                    )
                    ON CONFLICT (source_id) DO UPDATE
                    SET enabled_at = now(), enabled_note = EXCLUDED.enabled_note,
                        updated_at = now()
                    """
                ),
                {
                    "source_id": row["id"],
                    "note": (
                        "0127 owner demo recovery: positive resolved provider P/L "
                        "enables forward-only paper execution."
                    ),
                },
            )

        bind.execute(
            sa.text(
                """
                INSERT INTO audit_events(event_type, entity_type, entity_id, payload)
                VALUES(
                    'telegram.source_status_changed', 'source', :source_id,
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
                        "reason": "owner_profit_only_demo_recovery_20261005",
                    }
                ),
            },
        )


def downgrade() -> None:
    pass
