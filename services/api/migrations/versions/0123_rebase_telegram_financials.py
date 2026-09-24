"""Rebase the live Telegram rolling ledger onto the last verified good post.

Revision ID: 0123_rebase_telegram_financials
Revises: 0122_telegram_financial_ledger
Create Date: 2026-09-24

The 0122 rollout started mid-session, so its first state row began Today's P&L at zero.
The owner verified Telegram message 3745 as the correct anchor:
Balance 2570.29, Today's P&L +128.44.

Rebuild every 0122 financial row after that anchor using each publication's own event_delta,
so the visible Telegram sequence is arithmetic-only and restart-safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0123_rebase_telegram_financials"
down_revision: str | None = "0122_telegram_financial_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REFERENCE_USER_ID = "ea604df2-f8ee-47d1-bc51-f0078dbf160d"
DESTINATION_CHAT_ID = -5314636936
ANCHOR_TELEGRAM_MESSAGE_ID = 3745
ANCHOR_BALANCE = 2570.29
ANCHOR_DAILY_PNL = 128.44


def upgrade() -> None:
    bind = op.get_bind()

    anchor = bind.execute(
        sa.text(
            """
            SELECT sent_at
            FROM telegram_publications
            WHERE telegram_message_id=:message_id
              AND destination_chat_id=:chat_id
              AND status='sent'
            ORDER BY sent_at DESC
            LIMIT 1
            """
        ),
        {
            "message_id": ANCHOR_TELEGRAM_MESSAGE_ID,
            "chat_id": DESTINATION_CHAT_ID,
        },
    ).mappings().first()
    if anchor is None or anchor["sent_at"] is None:
        return

    business_date = bind.execute(
        sa.text("SELECT timezone('Europe/Sofia', :anchor_at)::date"),
        {"anchor_at": anchor["sent_at"]},
    ).scalar_one()

    # Rebase every financial row created by 0122. event_delta is immutable broker cash
    # for that publication; zero-delta posts simply inherit the previous published state.
    bind.execute(
        sa.text(
            """
            WITH ordered AS (
                SELECT
                    f.publication_id,
                    f.event_delta,
                    SUM(f.event_delta) OVER (
                        ORDER BY COALESCE(p.sent_at,p.created_at),p.id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                    ) AS cumulative_delta
                FROM telegram_publication_financials f
                JOIN telegram_publications p ON p.id=f.publication_id
                WHERE f.reference_user_id=:user_id
                  AND f.destination_chat_id=:chat_id
                  AND f.business_date=:business_date
                  AND f.status='sent'
                  AND p.sent_at>:anchor_at
            )
            UPDATE telegram_publication_financials f
            SET
                prior_balance=CAST(:anchor_balance AS numeric)
                    + ordered.cumulative_delta - ordered.event_delta,
                prior_daily_pnl=CAST(:anchor_daily AS numeric)
                    + ordered.cumulative_delta - ordered.event_delta,
                new_balance=CAST(:anchor_balance AS numeric) + ordered.cumulative_delta,
                new_daily_pnl=CAST(:anchor_daily AS numeric) + ordered.cumulative_delta,
                updated_at=now()
            FROM ordered
            WHERE f.publication_id=ordered.publication_id
            """
        ),
        {
            "user_id": REFERENCE_USER_ID,
            "chat_id": DESTINATION_CHAT_ID,
            "business_date": business_date,
            "anchor_at": anchor["sent_at"],
            "anchor_balance": ANCHOR_BALANCE,
            "anchor_daily": ANCHOR_DAILY_PNL,
        },
    )

    latest = bind.execute(
        sa.text(
            """
            SELECT
                f.publication_id,
                f.new_balance,
                f.new_daily_pnl
            FROM telegram_publication_financials f
            JOIN telegram_publications p ON p.id=f.publication_id
            WHERE f.reference_user_id=:user_id
              AND f.destination_chat_id=:chat_id
              AND f.business_date=:business_date
              AND f.status='sent'
              AND p.sent_at>:anchor_at
            ORDER BY p.sent_at DESC,p.id DESC
            LIMIT 1
            """
        ),
        {
            "user_id": REFERENCE_USER_ID,
            "chat_id": DESTINATION_CHAT_ID,
            "business_date": business_date,
            "anchor_at": anchor["sent_at"],
        },
    ).mappings().first()

    if latest is None:
        balance = ANCHOR_BALANCE
        daily = ANCHOR_DAILY_PNL
        last_publication_id = bind.execute(
            sa.text(
                """
                SELECT id
                FROM telegram_publications
                WHERE telegram_message_id=:message_id
                  AND destination_chat_id=:chat_id
                  AND status='sent'
                ORDER BY sent_at DESC
                LIMIT 1
                """
            ),
            {
                "message_id": ANCHOR_TELEGRAM_MESSAGE_ID,
                "chat_id": DESTINATION_CHAT_ID,
            },
        ).scalar_one_or_none()
    else:
        balance = latest["new_balance"]
        daily = latest["new_daily_pnl"]
        last_publication_id = latest["publication_id"]

    bind.execute(
        sa.text(
            """
            INSERT INTO telegram_financial_state(
                reference_user_id,destination_chat_id,business_date,
                balance,daily_pnl,last_publication_id,created_at,updated_at
            )
            VALUES(
                :user_id,:chat_id,:business_date,
                :balance,:daily,:last_publication_id,now(),now()
            )
            ON CONFLICT(reference_user_id,destination_chat_id,business_date)
            DO UPDATE SET
                balance=EXCLUDED.balance,
                daily_pnl=EXCLUDED.daily_pnl,
                last_publication_id=EXCLUDED.last_publication_id,
                updated_at=now()
            """
        ),
        {
            "user_id": REFERENCE_USER_ID,
            "chat_id": DESTINATION_CHAT_ID,
            "business_date": business_date,
            "balance": balance,
            "daily": daily,
            "last_publication_id": last_publication_id,
        },
    )


def downgrade() -> None:
    pass
