"""Add append-only AI message supervisor decisions.

Revision ID: 0013_ai_message_supervisor
Revises: 0012_mt5_accounts
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_ai_message_supervisor"
down_revision: str | None = "0012_mt5_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_message_decisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_index", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column("pipeline_action", sa.String(length=20), nullable=False),
        sa.Column("decision_source", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=50), nullable=False),
        sa.Column("message_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "structured_output",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("symbol", sa.String(length=40)),
        sa.Column("direction", sa.String(length=4)),
        sa.Column("order_type", sa.String(length=20)),
        sa.Column(
            "entry_prices",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("stop_loss", sa.Numeric(24, 10)),
        sa.Column(
            "take_profits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("open_runner", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("double_lot", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("update_action", sa.String(length=40)),
        sa.Column("target_index", sa.Integer()),
        sa.Column("new_stop_loss", sa.Numeric(24, 10)),
        sa.Column("stated_pips", sa.Numeric(24, 10)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "revision_index >= 0",
            name="ck_ai_message_decisions_revision_nonnegative",
        ),
        sa.CheckConstraint(
            "decision IN ('new_trade', 'trade_update', 'preparation', 'chatter', 'skip')",
            name="ck_ai_message_decisions_decision",
        ),
        sa.CheckConstraint(
            "pipeline_action IN ('proceed', 'ignore', 'skip')",
            name="ck_ai_message_decisions_pipeline_action",
        ),
        sa.CheckConstraint(
            "decision_source IN ('ai', 'deterministic_fallback')",
            name="ck_ai_message_decisions_source",
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('BUY', 'SELL')",
            name="ck_ai_message_decisions_direction",
        ),
        sa.CheckConstraint(
            "order_type IS NULL OR order_type IN ('market', 'limit', 'stop', 'unknown')",
            name="ck_ai_message_decisions_order_type",
        ),
        sa.UniqueConstraint(
            "message_id",
            "revision_index",
            name="uq_ai_message_decisions_message_revision",
        ),
    )
    op.create_index(
        "ix_ai_message_decisions_created",
        "ai_message_decisions",
        ["created_at"],
    )
    op.create_index(
        "ix_ai_message_decisions_decision_created",
        "ai_message_decisions",
        ["decision", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_ai_message_decision_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'ai_message_decisions are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ai_message_decisions_append_only
        BEFORE UPDATE OR DELETE ON ai_message_decisions
        FOR EACH ROW EXECUTE FUNCTION prevent_ai_message_decision_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ai_message_decisions_append_only ON ai_message_decisions")
    op.execute("DROP FUNCTION IF EXISTS prevent_ai_message_decision_mutation()")
    op.drop_index("ix_ai_message_decisions_decision_created", table_name="ai_message_decisions")
    op.drop_index("ix_ai_message_decisions_created", table_name="ai_message_decisions")
    op.drop_table("ai_message_decisions")
