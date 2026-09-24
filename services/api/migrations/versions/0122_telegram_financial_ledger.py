"""Persist the member Telegram rolling financial ledger.

Revision ID: 0122_telegram_financial_ledger
Revises: 0121_profit_only_active_sources
Create Date: 2026-09-24

Telegram money lines are a publication ledger:
* NEW TRADE / management / summary posts inherit the previous published state.
* Only a newly realised broker settlement changes Balance and Today's P&L.
* A publication may reserve a proposed state, but the durable state advances only
  after Telegram confirms the message was sent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0122_telegram_financial_ledger"
down_revision: str | None = "0121_profit_only_active_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_financial_state",
        sa.Column("reference_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("daily_pnl", sa.Numeric(18, 2), nullable=False),
        sa.Column("last_publication_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint(
            "reference_user_id",
            "destination_chat_id",
            "business_date",
            name="pk_telegram_financial_state",
        ),
    )

    op.create_table(
        "telegram_publication_financials",
        sa.Column(
            "publication_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("telegram_publications.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("reference_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("event_delta", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("prior_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("prior_daily_pnl", sa.Numeric(18, 2), nullable=False),
        sa.Column("new_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("new_daily_pnl", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="reserved"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved','sent','failed')",
            name="ck_telegram_publication_financials_status",
        ),
    )
    op.create_index(
        "ix_telegram_publication_financials_state",
        "telegram_publication_financials",
        ["reference_user_id", "destination_chat_id", "business_date", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telegram_publication_financials_state",
        table_name="telegram_publication_financials",
    )
    op.drop_table("telegram_publication_financials")
    op.drop_table("telegram_financial_state")
