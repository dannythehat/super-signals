"""Activate the two shadow sources positive in both October and all-time broker P&L.

Revision ID: 0128_oct_positive
Revises: 0127_profit_only_demo
Create Date: 2026-10-22

Owner-approved basket: seven providers profitable in October 2026 AND overall
by broker-confirmed closed-position cash P&L. Five are already in testing and
must not be modified; only the two exact shadow sources below are promoted.

This only enables future owner/reference paper routing by setting testing.
It does not graduate sources, grant member LIVE distribution, alter sizing,
change any other provider, or retry/replay older signals or orders.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0128_oct_positive"
down_revision: str | None = "0127_profit_only_demo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Exact database IDs AND Telegram chat IDs. Do not select by similar titles.
_SHADOW_TO_TESTING = (
    ("4b214aaf-dc12-4977-85b1-d269d17716f3", -1001391285381, "GOLD TRADING PK"),
    ("13af029e-5491-4bba-80e8-13c74b360d31", -1002144761347, "TOP 1% TRADES 🥇"),
)

_NOTE = (
    "Owner requested future-only reference/demo trading on 22 October 2026: "
    "broker-confirmed closed P&L positive for both all time and October; "
    "member LIVE excluded until separate explicit graduation."
)


def upgrade() -> None:
    bind = op.get_bind()
    # Check ALL sources before changing ANY, so a missing or revoked source
    # cannot cause an incomplete basket.
    verified = []
    for source_id, chat_id, title in _SHADOW_TO_TESTING:
        row = bind.execute(
            sa.text(
                """
                SELECT s.id, s.status, s.source_alias,
                       COALESCE(SUM(o.cash_pnl) FILTER (
                           WHERE o.broker_deal_count>0
                             AND o.status IN ('won','lost','breakeven','closed_unknown')
                       ),0) AS all_time_pnl,
                       COALESCE(SUM(o.cash_pnl) FILTER (
                           WHERE o.broker_deal_count>0
                             AND o.status IN ('won','lost','breakeven','closed_unknown')
                             AND o.closed_at >= TIMESTAMPTZ '2026-10-01 00:00:00+00'
                             AND o.closed_at < TIMESTAMPTZ '2026-11-01 00:00:00+00'
                       ),0) AS october_pnl,
                       (SELECT pep.graduated
                        FROM provider_execution_probation pep
                        WHERE pep.source_id=s.id) AS graduated
                FROM sources s
                LEFT JOIN performance_trade_outcomes o ON o.source_id=s.id
                WHERE s.id=CAST(:source_id AS uuid) AND s.chat_id=:chat_id
                GROUP BY s.id
                """
            ),
            {"source_id": source_id, "chat_id": chat_id},
        ).mappings().one_or_none()
        if row is None or row["status"] not in ("shadow", "testing"):
            raise RuntimeError(f"Provider missing or no longer eligible: {title}")
        if bool(row["graduated"]):
            raise RuntimeError(f"Provider already graduated to member LIVE: {title}")
        if (
            Decimal(str(row["all_time_pnl"])) <= 0
            or Decimal(str(row["october_pnl"])) <= 0
        ):
            raise RuntimeError(f"Provider no longer profitable October and all-time: {title}")
        verified.append((row, source_id, title))

    for row, source_id, title in verified:
        if row["status"] == "testing":
            continue  # idempotent, do not rewrite an existing live-source state

        changed = bind.execute(
            sa.text(
                """
                UPDATE sources SET status='testing',updated_at=now()
                WHERE id=CAST(:source_id AS uuid) AND status='shadow'
                RETURNING id
                """
            ),
            {"source_id": source_id},
        ).scalar_one_or_none()
        if changed is None:
            raise RuntimeError(f"Source status changed concurrently: {title}")

        bind.execute(
            sa.text(
                """
                INSERT INTO provider_execution_probation (
                    source_id,enabled_at,enabled_note,graduated,graduated_at,
                    created_at,updated_at
                )
                VALUES (
                    CAST(:source_id AS uuid),now(),:note,false,NULL,now(),now()
                )
                ON CONFLICT(source_id) DO NOTHING
                """
            ),
            {"source_id": source_id, "note": _NOTE},
        )
        payload = {
            "source_title": title,
            "previous_status": "shadow",
            "status": "testing",
            "paper_execution_enabled": True,
            "live_member_distribution_enabled": False,
            "historical_replay_enabled": False,
            "all_time_broker_pnl_usd": float(row["all_time_pnl"]),
            "october_broker_pnl_usd": float(row["october_pnl"]),
            "reason": "owner_approved_october_and_all_time_profitable_basket_20261022",
        }
        bind.execute(
            sa.text(
                """
                INSERT INTO audit_events(event_type,entity_type,entity_id,payload)
                VALUES (
                    'telegram.source_status_changed','source',
                    CAST(:source_id AS uuid),CAST(:payload AS jsonb)
                )
                """
            ),
            {"source_id": source_id, "payload": json.dumps(payload)},
        )


def downgrade() -> None:
    # Avoid silently undoing any trading decision made after this migration.
    pass
