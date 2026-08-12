"""Add canonical broker-backed performance ledger and summaries.

Revision ID: 0018_day33_performance_ledger
Revises: 0017_day31_trading_controls
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_day33_performance_ledger"
down_revision: str | None = "0017_day31_trading_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "broker_deals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "mt5_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mt5_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("signals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trader_stream", sa.String(length=80), nullable=True),
        sa.Column("broker_deal_id", sa.String(length=120), nullable=False),
        sa.Column("broker_position_id", sa.String(length=120), nullable=True),
        sa.Column("broker_order_id", sa.String(length=120), nullable=True),
        sa.Column("broker_client_id", sa.String(length=64), nullable=True),
        sa.Column("deal_type", sa.String(length=64), nullable=False),
        sa.Column("entry_type", sa.String(length=32), nullable=True),
        sa.Column("symbol", sa.String(length=40), nullable=True),
        sa.Column("volume", sa.Numeric(18, 8), nullable=True),
        sa.Column("price", sa.Numeric(24, 10), nullable=True),
        sa.Column("profit", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("commission", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("swap", sa.Numeric(18, 2), nullable=False, server_default="0"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("broker_time", sa.String(length=40), nullable=True),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "mt5_account_id",
            "broker_deal_id",
            name="uq_broker_deals_account_deal",
        ),
    )
    op.create_index(
        "ix_broker_deals_user_time",
        "broker_deals",
        ["user_id", "occurred_at"],
    )
    op.create_index(
        "ix_broker_deals_position",
        "broker_deals",
        ["position_id", "occurred_at"],
    )
    op.execute("ALTER TABLE broker_deals ENABLE ROW LEVEL SECURITY")

    op.create_table(
        "performance_account_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "mt5_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mt5_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=12), nullable=False),
        sa.Column("balance", sa.Numeric(20, 2), nullable=False),
        sa.Column("equity", sa.Numeric(20, 2), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "mt5_account_id",
            "captured_at",
            name="uq_performance_snapshots_account_time",
        ),
    )
    op.create_index(
        "ix_performance_snapshots_user_time",
        "performance_account_snapshots",
        ["user_id", "captured_at"],
    )
    op.execute("ALTER TABLE performance_account_snapshots ENABLE ROW LEVEL SECURITY")

    op.create_table(
        "performance_trade_outcomes",
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("positions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("signals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trader_stream", sa.String(length=80), nullable=True),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_price", sa.Numeric(24, 10), nullable=True),
        sa.Column("exit_price", sa.Numeric(24, 10), nullable=True),
        sa.Column("volume", sa.Numeric(18, 8), nullable=True),
        sa.Column("cash_pnl", sa.Numeric(18, 2), nullable=True),
        sa.Column("return_percent", sa.Numeric(12, 6), nullable=True),
        sa.Column("net_pips", sa.Numeric(18, 2), nullable=True),
        sa.Column("pip_size", sa.Numeric(24, 10), nullable=True),
        sa.Column("model_500_pnl", sa.Numeric(18, 2), nullable=True),
        sa.Column("model_500_return_percent", sa.Numeric(12, 6), nullable=True),
        sa.Column("planned_risk_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("close_reason", sa.String(length=80), nullable=True),
        sa.Column("broker_deal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "derived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('open','pending','won','lost','breakeven','closed_unknown')",
            name="ck_performance_trade_outcomes_status",
        ),
    )
    op.create_index(
        "ix_performance_outcomes_user_closed",
        "performance_trade_outcomes",
        ["user_id", "closed_at"],
    )
    op.create_index(
        "ix_performance_outcomes_source",
        "performance_trade_outcomes",
        ["source_id", "trader_stream", "closed_at"],
    )
    op.execute("ALTER TABLE performance_trade_outcomes ENABLE ROW LEVEL SECURITY")

    op.create_table(
        "performance_summaries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("period_type", sa.String(length=16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dimension_type", sa.String(length=16), nullable=False),
        sa.Column("dimension_key", sa.String(length=200), nullable=False),
        sa.Column("dimension_label", sa.String(length=200), nullable=False),
        sa.Column("total_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("breakeven", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_trades", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cash_pnl", sa.Numeric(20, 2), nullable=False, server_default="0"),
        sa.Column("return_percent", sa.Numeric(12, 6), nullable=True),
        sa.Column("net_pips", sa.Numeric(20, 2), nullable=True),
        sa.Column("gross_profit_pips", sa.Numeric(20, 2), nullable=True),
        sa.Column("gross_loss_pips", sa.Numeric(20, 2), nullable=True),
        sa.Column("model_500_pnl", sa.Numeric(20, 2), nullable=False, server_default="0"),
        sa.Column("model_500_return_percent", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "period_type IN ('daily','weekly','monthly','yearly','all_time')",
            name="ck_performance_summaries_period_type",
        ),
        sa.CheckConstraint(
            "dimension_type IN ('portfolio','source','trader','symbol')",
            name="ck_performance_summaries_dimension_type",
        ),
        sa.UniqueConstraint(
            "user_id",
            "period_type",
            "period_start",
            "dimension_type",
            "dimension_key",
            name="uq_performance_summaries_period_dimension",
        ),
    )
    op.create_index(
        "ix_performance_summaries_user_period",
        "performance_summaries",
        ["user_id", "period_type", "period_start"],
    )
    op.execute("ALTER TABLE performance_summaries ENABLE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_broker_deal_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'broker_deals are immutable';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER broker_deals_immutable
        BEFORE UPDATE OR DELETE ON broker_deals
        FOR EACH ROW EXECUTE FUNCTION reject_broker_deal_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS broker_deals_immutable ON broker_deals")
    op.execute("DROP FUNCTION IF EXISTS reject_broker_deal_mutation()")
    op.drop_index("ix_performance_summaries_user_period", table_name="performance_summaries")
    op.drop_table("performance_summaries")
    op.drop_index("ix_performance_outcomes_source", table_name="performance_trade_outcomes")
    op.drop_index("ix_performance_outcomes_user_closed", table_name="performance_trade_outcomes")
    op.drop_table("performance_trade_outcomes")
    op.drop_index("ix_performance_snapshots_user_time", table_name="performance_account_snapshots")
    op.drop_table("performance_account_snapshots")
    op.drop_index("ix_broker_deals_position", table_name="broker_deals")
    op.drop_index("ix_broker_deals_user_time", table_name="broker_deals")
    op.drop_table("broker_deals")
