"""Add canonical signal lifecycle events and reply-linked publications.

Revision ID: 0011_signal_lifecycle_events
Revises: 0010_telegram_publications
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_signal_lifecycle_events"
down_revision: str | None = "0010_telegram_publications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signal_lifecycle_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("signals.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("source_revision_index", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("origin", sa.String(length=24), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("pips", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "aggregate_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "source_revision_index IS NULL OR source_revision_index >= 0",
            name="ck_signal_lifecycle_revision_nonnegative",
        ),
        sa.CheckConstraint(
            "origin IN ('provider_update', 'system', 'broker', 'acceptance_test')",
            name="ck_signal_lifecycle_origin",
        ),
        sa.CheckConstraint(
            "length(trim(event_type)) > 0 AND length(trim(event_key)) > 0",
            name="ck_signal_lifecycle_identity_nonempty",
        ),
        sa.UniqueConstraint("event_key", name="uq_signal_lifecycle_event_key"),
    )
    op.create_index(
        "ix_signal_lifecycle_signal_occurred",
        "signal_lifecycle_events",
        ["signal_id", "occurred_at", "created_at"],
    )
    op.create_index(
        "ix_signal_lifecycle_source_message",
        "signal_lifecycle_events",
        ["source_message_id", "source_revision_index"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_signal_lifecycle_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'signal_lifecycle_events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_signal_lifecycle_append_only
        BEFORE UPDATE OR DELETE ON signal_lifecycle_events
        FOR EACH ROW EXECUTE FUNCTION prevent_signal_lifecycle_mutation()
        """
    )

    op.add_column(
        "telegram_publications",
        sa.Column(
            "lifecycle_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("signal_lifecycle_events.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "telegram_publications",
        sa.Column("reply_to_telegram_message_id", sa.BigInteger(), nullable=True),
    )
    op.drop_constraint(
        "uq_telegram_publications_signal_kind",
        "telegram_publications",
        type_="unique",
    )
    op.create_index(
        "uq_telegram_publications_signal_created",
        "telegram_publications",
        ["signal_id"],
        unique=True,
        postgresql_where=sa.text(
            "publication_kind = 'signal_created' AND lifecycle_event_id IS NULL"
        ),
    )
    op.create_index(
        "uq_telegram_publications_lifecycle_event",
        "telegram_publications",
        ["lifecycle_event_id"],
        unique=True,
        postgresql_where=sa.text("lifecycle_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM telegram_publications WHERE lifecycle_event_id IS NOT NULL"
    )
    op.drop_index(
        "uq_telegram_publications_lifecycle_event",
        table_name="telegram_publications",
    )
    op.drop_index(
        "uq_telegram_publications_signal_created",
        table_name="telegram_publications",
    )
    op.create_unique_constraint(
        "uq_telegram_publications_signal_kind",
        "telegram_publications",
        ["signal_id", "publication_kind"],
    )
    op.drop_column("telegram_publications", "reply_to_telegram_message_id")
    op.drop_column("telegram_publications", "lifecycle_event_id")

    op.execute(
        "DROP TRIGGER IF EXISTS trg_signal_lifecycle_append_only ON signal_lifecycle_events"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_signal_lifecycle_mutation()")
    op.drop_index(
        "ix_signal_lifecycle_source_message",
        table_name="signal_lifecycle_events",
    )
    op.drop_index(
        "ix_signal_lifecycle_signal_occurred",
        table_name="signal_lifecycle_events",
    )
    op.drop_table("signal_lifecycle_events")
