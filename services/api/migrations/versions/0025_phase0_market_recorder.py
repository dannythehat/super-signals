"""Add the Phase 0-lite point-in-time market memory tables.

Revision ID: 0025_phase0_market_recorder
Revises: 0024_day40_no_global_stop
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_phase0_market_recorder"
down_revision: str | None = "0024_day40_no_global_stop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PHASE0_TIMEFRAMES = "'1m','5m','15m','1h','4h','1d'"


def upgrade() -> None:
    op.create_table(
        "market_candles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("broker_time", sa.String(length=40), nullable=True),
        sa.Column("open", sa.Numeric(24, 10), nullable=False),
        sa.Column("high", sa.Numeric(24, 10), nullable=False),
        sa.Column("low", sa.Numeric(24, 10), nullable=False),
        sa.Column("close", sa.Numeric(24, 10), nullable=False),
        sa.Column("tick_volume", sa.BigInteger(), nullable=True),
        sa.Column("spread", sa.Numeric(24, 10), nullable=True),
        sa.Column("volume", sa.Numeric(24, 10), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            f"timeframe IN ({_PHASE0_TIMEFRAMES})",
            name="ck_market_candles_timeframe",
        ),
        sa.UniqueConstraint(
            "source",
            "symbol",
            "timeframe",
            "open_time",
            name="uq_market_candles_identity",
        ),
    )
    op.create_index(
        "ix_market_candles_lookup",
        "market_candles",
        ["symbol", "timeframe", "open_time"],
    )

    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "reference_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "mt5_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mt5_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("capture_status", sa.String(length=20), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quote_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bid", sa.Numeric(24, 10), nullable=True),
        sa.Column("ask", sa.Numeric(24, 10), nullable=True),
        sa.Column("balance", sa.Numeric(24, 10), nullable=True),
        sa.Column("equity", sa.Numeric(24, 10), nullable=True),
        sa.Column("margin", sa.Numeric(24, 10), nullable=True),
        sa.Column("free_margin", sa.Numeric(24, 10), nullable=True),
        sa.Column("trade_allowed", sa.Boolean(), nullable=True),
        sa.Column(
            "positions_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "orders_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "capture_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_stage", sa.String(length=80), nullable=True),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "capture_status IN ('complete','partial','failed')",
            name="ck_market_snapshots_capture_status",
        ),
    )
    op.create_index(
        "ix_market_snapshots_captured_at",
        "market_snapshots",
        ["captured_at"],
    )
    op.create_index(
        "ix_market_snapshots_symbol_time",
        "market_snapshots",
        ["symbol", "captured_at"],
    )

    op.create_table(
        "market_event_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("external_event_id", sa.String(length=200), nullable=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("country", sa.String(length=80), nullable=True),
        sa.Column("importance", sa.SmallInteger(), nullable=True),
        sa.Column("scheduled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("actual_text", sa.Text(), nullable=True),
        sa.Column("consensus_text", sa.Text(), nullable=True),
        sa.Column("previous_text", sa.Text(), nullable=True),
        sa.Column("revision_text", sa.Text(), nullable=True),
        sa.Column(
            "raw_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("observation_digest", sa.String(length=64), nullable=False),
        sa.UniqueConstraint(
            "source",
            "observation_digest",
            name="uq_market_event_observation_digest",
        ),
    )
    op.create_index(
        "ix_market_event_observations_observed",
        "market_event_observations",
        ["observed_at"],
    )
    op.create_index(
        "ix_market_event_observations_occurred",
        "market_event_observations",
        ["occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_event_observations_occurred",
        table_name="market_event_observations",
    )
    op.drop_index(
        "ix_market_event_observations_observed",
        table_name="market_event_observations",
    )
    op.drop_table("market_event_observations")

    op.drop_index("ix_market_snapshots_symbol_time", table_name="market_snapshots")
    op.drop_index("ix_market_snapshots_captured_at", table_name="market_snapshots")
    op.drop_table("market_snapshots")

    op.drop_index("ix_market_candles_lookup", table_name="market_candles")
    op.drop_table("market_candles")
