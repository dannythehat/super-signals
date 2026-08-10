"""Add append-only XAUUSD parser evidence.

Revision ID: 0007_message_parses
Revises: 0006_message_classifications
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_message_parses"
down_revision: str | None = "0006_message_classifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_parses",
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
        sa.Column("parse_status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "matched_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("parsed_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=40), nullable=False),
        sa.Column("symbol", sa.String(length=40)),
        sa.Column("direction", sa.String(length=4)),
        sa.Column("entry_price", sa.Numeric(24, 10)),
        sa.Column("stop_loss", sa.Numeric(24, 10)),
        sa.Column(
            "take_profits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("size_multiplier", sa.Numeric(8, 4)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "parse_status IN ('parsed', 'failed')",
            name="ck_message_parses_status",
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('BUY', 'SELL')",
            name="ck_message_parses_direction",
        ),
        sa.CheckConstraint(
            "size_multiplier IS NULL OR size_multiplier > 0",
            name="ck_message_parses_size_multiplier",
        ),
        sa.UniqueConstraint(
            "message_id",
            "revision_index",
            name="uq_message_parses_message_revision",
        ),
    )
    op.create_index(
        "ix_message_parses_message_created",
        "message_parses",
        ["message_id", "created_at"],
    )
    op.create_index(
        "ix_message_parses_status_created",
        "message_parses",
        ["parse_status", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_message_parse_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'message_parses are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_message_parses_append_only
        BEFORE UPDATE OR DELETE ON message_parses
        FOR EACH ROW EXECUTE FUNCTION prevent_message_parse_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_message_parses_append_only ON message_parses")
    op.execute("DROP FUNCTION IF EXISTS prevent_message_parse_mutation()")
    op.drop_index("ix_message_parses_status_created", table_name="message_parses")
    op.drop_index("ix_message_parses_message_created", table_name="message_parses")
    op.drop_table("message_parses")
