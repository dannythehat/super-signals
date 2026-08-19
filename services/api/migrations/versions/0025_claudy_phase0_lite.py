"""Add Claudy Phase 0-lite point-in-time market recorder tables.

Revision ID: 0025_claudy_phase0_lite
Revises: 0024_day40_no_global_stop
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_claudy_phase0_lite"
down_revision: str | None = "0024_day40_no_global_stop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMEFRAMES = "'1m','5m','15m','1h','4h','1d'"


def upgrade() -> None:
    op.create_table(
        "market_candles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("open_time_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("broker_open_time", sa.String(length=40), nullable=True),
        sa.Column("open", sa.Numeric(24, 10), nullable=False),
        sa.Column("high", sa.Numeric(24, 10), nullable=False),
        sa.Column("low", sa.Numeric(24, 10), nullable=False),
        sa.Column("close", sa.Numeric(24, 10), nullable=False),
        sa.Column("tick_volume", sa.BigInteger(), nullable=True),
        sa.Column("spread", sa.Numeric(24, 10), nullable=True),
        sa.Column("volume", sa.Numeric(24, 8), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="metaapi"),
        sa.Column("revision_index", sa.Integer(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "first_observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"timeframe IN ({_TIMEFRAMES})",
            name="ck_market_candles_timeframe",
        ),
        sa.CheckConstraint(
            "revision_index >= 1",
            name="ck_market_candles_revision",
        ),
        sa.UniqueConstraint(
            "source",
            "symbol",
            "timeframe",
            "open_time_utc",
            "revision_index",
            name="uq_market_candles_revision",
        ),
        sa.UniqueConstraint(
            "source",
            "symbol",
            "timeframe",
            "open_time_utc",
            "payload_digest",
            name="uq_market_candles_payload",
        ),
    )
    op.create_index(
        "ix_market_candles_symbol_timeframe_open",
        "market_candles",
        ["symbol", "timeframe", "open_time_utc"],
    )

    op.create_table(
        "market_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbol", sa.String(length=40), nullable=False),
        sa.Column("capture_status", sa.String(length=20), nullable=False),
        sa.Column("bid", sa.Numeric(24, 10), nullable=True),
        sa.Column("ask", sa.Numeric(24, 10), nullable=True),
        sa.Column("mid", sa.Numeric(24, 10), nullable=True),
        sa.Column("spread", sa.Numeric(24, 10), nullable=True),
        sa.Column("quote_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quote_age_seconds", sa.Numeric(18, 3), nullable=True),
        sa.Column("session_code", sa.String(length=40), nullable=False),
        sa.Column("terminal_trade_allowed", sa.Boolean(), nullable=True),
        # Nullable on purpose: NULL means position state was not known at capture time;
        # [] means the broker read succeeded and returned no positions.
        sa.Column(
            "position_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        # Nullable on purpose: NULL means "not captured", which is not the same claim as
        # an empty array ("captured, and there were none").
        sa.Column(
            "order_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "cross_market_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "provider_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "claudy_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "data_availability_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "event_observation_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "latest_m1_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_candles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "latest_m5_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_candles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "latest_m15_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_candles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "latest_h1_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_candles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "latest_h4_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_candles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "latest_d1_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_candles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("snapshot_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "capture_status IN ('complete','partial','unavailable')",
            name="ck_market_snapshots_status",
        ),
    )
    op.create_index(
        "ix_market_snapshots_symbol_captured",
        "market_snapshots",
        ["symbol", "captured_at"],
    )

    op.create_table(
        "market_event_observations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revision_index", sa.Integer(), nullable=False),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column(
            "structured_data_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "raw_payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "revision_index >= 1",
            name="ck_market_event_observations_revision",
        ),
        sa.UniqueConstraint(
            "source",
            "external_id",
            "revision_index",
            name="uq_market_event_observations_revision",
        ),
        sa.UniqueConstraint(
            "source",
            "external_id",
            "payload_digest",
            name="uq_market_event_observations_payload",
        ),
    )
    op.create_index(
        "ix_market_event_observations_published",
        "market_event_observations",
        ["published_at"],
    )
    op.create_index(
        "ix_market_event_observations_first_observed",
        "market_event_observations",
        ["first_observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_market_event_observations_first_observed",
        table_name="market_event_observations",
    )
    op.drop_index(
        "ix_market_event_observations_published",
        table_name="market_event_observations",
    )
    op.drop_table("market_event_observations")
    op.drop_index("ix_market_snapshots_symbol_captured", table_name="market_snapshots")
    op.drop_table("market_snapshots")
    op.drop_index(
        "ix_market_candles_symbol_timeframe_open",
        table_name="market_candles",
    )
    op.drop_table("market_candles")
