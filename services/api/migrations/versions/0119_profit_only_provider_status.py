"""Apply the owner's profit-only provider execution policy.

Revision ID: 0119_profit_only_provider_status
Revises: 0118_shadow_scalping
Create Date: 2026-09-24

Policy for the existing active/shadow provider basket:
* provider scoreboard P/L > 0 with resolved evidence -> Testing (owner execution ON)
* provider scoreboard P/L <= 0, missing, or unresolved -> Shadow
* no provider-name, style, cadence, or "scalper" exception may override this status
* active providers are probationary for member LIVE distribution unless already graduated;
  the owner reference/demo account still executes both BUY and SELL.

Paused/revoked sources are deliberately untouched. Historical signals are not replayed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0119_profit_only_provider_status"
down_revision: str | None = "0118_shadow_scalping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOTE = (
    "0119 profit-only provider policy: positive scoreboard P/L enables owner/demo "
    "forward execution; non-positive or insufficient evidence remains shadow. "
    "Member LIVE stays excluded until explicit graduation."
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT
                s.id,
                s.status,
                COALESCE(NULLIF(s.chat_title, ''), s.source_alias, s.id::text) AS title,
                COALESCE(ps.net_pnl_usd, 0) AS net_pnl_usd,
                COALESCE(ps.trades_resolved, 0) AS trades_resolved
            FROM sources AS s
            LEFT JOIN provider_trade_scoreboard AS ps ON ps.source_id = s.id
            WHERE s.status IN ('shadow', 'testing')
            ORDER BY s.created_at
            """
        )
    ).mappings().all()

    for row in rows:
        pnl = row["net_pnl_usd"]
        resolved = int(row["trades_resolved"] or 0)
        wanted = "testing" if pnl is not None and pnl > 0 and resolved > 0 else "shadow"
        previous = str(row["status"])

        if wanted == "testing":
            bind.execute(
                sa.text(
                    """
                    INSERT INTO provider_execution_probation (
                        source_id, enabled_at, enabled_note, graduated, graduated_at,
                        created_at, updated_at
                    )
                    VALUES (:source_id, now(), :note, false, NULL, now(), now())
                    ON CONFLICT (source_id) DO NOTHING
                    """
                ),
                {"source_id": row["id"], "note": _NOTE},
            )

        if previous == wanted:
            continue

        bind.execute(
            sa.text(
                """
                UPDATE sources
                SET status=:status, updated_at=now()
                WHERE id=:source_id
                """
            ),
            {"status": wanted, "source_id": row["id"]},
        )

        payload = json.dumps(
            {
                "source_title": str(row["title"]),
                "previous_status": previous,
                "status": wanted,
                "actor_role": "owner_instruction_migration",
                "actor_display_name": "Super Signals",
                "paper_execution_enabled": wanted == "testing",
                "historical_replay_enabled": False,
                "provider_scoreboard_pnl_usd": float(pnl or 0),
                "provider_scoreboard_resolved_trades": resolved,
                "reason": "owner_profit_only_provider_policy",
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
    pass
