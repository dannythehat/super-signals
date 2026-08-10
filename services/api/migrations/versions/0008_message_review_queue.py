"""Add strict validation evidence and admin review queue.

Revision ID: 0008_message_review_queue
Revises: 0007_message_parses
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_message_review_queue"
down_revision: str | None = "0007_message_parses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_validations",
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
        sa.Column("validation_status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "matched_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("validated_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("validator_version", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "validation_status IN ('valid', 'failed')",
            name="ck_message_validations_status",
        ),
        sa.UniqueConstraint(
            "message_id",
            "revision_index",
            name="uq_message_validations_message_revision",
        ),
    )
    op.create_index(
        "ix_message_validations_status_created",
        "message_validations",
        ["validation_status", "created_at"],
    )

    op.create_table(
        "message_review_items",
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
        sa.Column("review_stage", sa.String(length=20), nullable=False),
        sa.Column("review_status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "matched_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("raw_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("classification", sa.String(length=30)),
        sa.Column("decision_status", sa.String(length=20)),
        sa.Column("classifier_version", sa.String(length=40)),
        sa.Column("parse_status", sa.String(length=20)),
        sa.Column("parser_version", sa.String(length=40)),
        sa.Column("validator_version", sa.String(length=40)),
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
            "review_stage IN ('classification', 'parser', 'validation')",
            name="ck_message_review_items_stage",
        ),
        sa.CheckConstraint(
            "review_status = 'open'",
            name="ck_message_review_items_status",
        ),
        sa.CheckConstraint(
            "direction IS NULL OR direction IN ('BUY', 'SELL')",
            name="ck_message_review_items_direction",
        ),
        sa.UniqueConstraint(
            "message_id",
            "revision_index",
            name="uq_message_review_items_message_revision",
        ),
    )
    op.create_index(
        "ix_message_review_items_created",
        "message_review_items",
        ["created_at"],
    )
    op.create_index(
        "ix_message_review_items_stage_created",
        "message_review_items",
        ["review_stage", "created_at"],
    )

    op.execute(
        """
        CREATE FUNCTION prevent_message_validation_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'message_validations are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_message_validations_append_only
        BEFORE UPDATE OR DELETE ON message_validations
        FOR EACH ROW EXECUTE FUNCTION prevent_message_validation_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION prevent_message_review_item_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'message_review_items are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_message_review_items_append_only
        BEFORE UPDATE OR DELETE ON message_review_items
        FOR EACH ROW EXECUTE FUNCTION prevent_message_review_item_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_message_review_items_append_only ON message_review_items")
    op.execute("DROP FUNCTION IF EXISTS prevent_message_review_item_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_message_validations_append_only ON message_validations")
    op.execute("DROP FUNCTION IF EXISTS prevent_message_validation_mutation()")
    op.drop_index("ix_message_review_items_stage_created", table_name="message_review_items")
    op.drop_index("ix_message_review_items_created", table_name="message_review_items")
    op.drop_table("message_review_items")
    op.drop_index("ix_message_validations_status_created", table_name="message_validations")
    op.drop_table("message_validations")
