"""Add append-only message classification history.

Revision ID: 0006_message_classifications
Revises: 0005_message_revisions
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_message_classifications"
down_revision: str | None = "0005_message_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_classifications",
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
        sa.Column("classification", sa.String(length=20), nullable=False),
        sa.Column("decision_status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "matched_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("classified_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("classifier_version", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "classification IN ('new_trade', 'trade_update', 'chatter', 'uncertain')",
            name="ck_message_classifications_category",
        ),
        sa.CheckConstraint(
            "decision_status IN ('classified', 'ignored', 'review')",
            name="ck_message_classifications_decision",
        ),
        sa.UniqueConstraint(
            "message_id",
            "revision_index",
            name="uq_message_classifications_message_revision",
        ),
    )
    op.create_index(
        "ix_message_classifications_message_created",
        "message_classifications",
        ["message_id", "created_at"],
    )
    op.create_index(
        "ix_message_classifications_category_created",
        "message_classifications",
        ["classification", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_message_classification_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'message_classifications are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_message_classifications_append_only
        BEFORE UPDATE OR DELETE ON message_classifications
        FOR EACH ROW EXECUTE FUNCTION prevent_message_classification_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_message_classifications_append_only ON message_classifications"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_message_classification_mutation()")
    op.drop_index(
        "ix_message_classifications_category_created",
        table_name="message_classifications",
    )
    op.drop_index(
        "ix_message_classifications_message_created",
        table_name="message_classifications",
    )
    op.drop_table("message_classifications")
