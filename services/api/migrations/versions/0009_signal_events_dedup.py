"""Add canonical Signal identity and duplicate-observation evidence.

Revision ID: 0009_signal_events_dedup
Revises: 0008_message_review_queue
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_signal_events_dedup"
down_revision: str | None = "0008_message_review_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "signals",
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column("signals", sa.Column("provider_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("signals", sa.Column("provider_message_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "signals",
        sa.Column(
            "source_revision_index",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "signals",
        sa.Column("source_posted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "signals",
        sa.Column(
            "take_profits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "signals",
        sa.Column("signal_fingerprint", sa.String(length=64), nullable=True),
    )

    # The accepted live database enters Day 18 with zero Signals. This legacy-safe
    # backfill still makes the migration deterministic if an older environment has rows.
    op.execute(
        """
        UPDATE signals AS sig
        SET source_id = m.source_id,
            provider_chat_id = s.chat_id,
            provider_message_id = m.telegram_message_id,
            source_posted_at = m.posted_at,
            signal_fingerprint = md5(sig.id::text || ':' || sig.source_message_id::text)
                               || md5(sig.source_message_id::text || ':' || sig.id::text)
        FROM messages AS m
        JOIN sources AS s ON s.id = m.source_id
        WHERE m.id = sig.source_message_id
          AND sig.signal_fingerprint IS NULL
        """
    )

    op.alter_column("signals", "source_id", nullable=False)
    op.alter_column("signals", "provider_chat_id", nullable=False)
    op.alter_column("signals", "provider_message_id", nullable=False)
    op.alter_column("signals", "source_posted_at", nullable=False)
    op.alter_column("signals", "signal_fingerprint", nullable=False)
    op.create_check_constraint(
        "ck_signals_source_revision_nonnegative",
        "signals",
        "source_revision_index >= 0",
    )
    op.create_index(
        "uq_signals_provider_message",
        "signals",
        ["provider_chat_id", "provider_message_id"],
        unique=True,
    )
    op.create_index(
        "uq_signals_fingerprint",
        "signals",
        ["signal_fingerprint"],
        unique=True,
    )

    op.create_table(
        "signal_observations",
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
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("disposition", sa.String(length=20), nullable=False),
        sa.Column("observed_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "revision_index >= 0",
            name="ck_signal_observations_revision_nonnegative",
        ),
        sa.CheckConstraint(
            "disposition IN ('canonical', 'duplicate')",
            name="ck_signal_observations_disposition",
        ),
        sa.UniqueConstraint(
            "message_id",
            "revision_index",
            name="uq_signal_observations_message_revision",
        ),
    )
    op.create_index(
        "ix_signal_observations_signal_created",
        "signal_observations",
        ["signal_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_signal_observations_signal_created", table_name="signal_observations")
    op.drop_table("signal_observations")
    op.drop_index("uq_signals_fingerprint", table_name="signals")
    op.drop_index("uq_signals_provider_message", table_name="signals")
    op.drop_constraint("ck_signals_source_revision_nonnegative", "signals", type_="check")
    op.drop_column("signals", "signal_fingerprint")
    op.drop_column("signals", "take_profits")
    op.drop_column("signals", "source_posted_at")
    op.drop_column("signals", "source_revision_index")
    op.drop_column("signals", "provider_message_id")
    op.drop_column("signals", "provider_chat_id")
    op.drop_column("signals", "source_id")
