"""Persist terminal AIDY provider-context misses so they are never retried forever.

Revision ID: 0064_provider_context_terminal
Revises: 0063_provider_day14_governance
Create Date: 2026-09-08

A historical signal that receives AIDY's `pit_context_stale` response cannot become
point-in-time valid later without rewriting historical capture truth.  Persist that
research-only outcome once and exclude it from subsequent polling.  This migration
creates no broker/member execution path and grants no live-money authority.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064_provider_context_terminal"
down_revision: str | None = "0063_provider_day14_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE = "provider_signal_context_terminal_misses"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("contract_version", sa.String(length=48), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("research_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "live_money_execution_allowed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("signal_id", name="uq_provider_context_terminal_miss_signal"),
        sa.CheckConstraint(
            "reason = 'pit_context_stale'",
            name="ck_provider_context_terminal_miss_reason",
        ),
        sa.CheckConstraint(
            "COALESCE(response_payload->>'error','') = reason",
            name="ck_provider_context_terminal_miss_payload_reason",
        ),
        sa.CheckConstraint(
            "research_only",
            name="ck_provider_context_terminal_miss_research_only",
        ),
        sa.CheckConstraint(
            "NOT live_money_execution_allowed",
            name="ck_provider_context_terminal_miss_no_live_money",
        ),
    )
    op.create_index(
        "ix_provider_context_terminal_miss_source_posted",
        TABLE,
        ["source_id", "signal_posted_at"],
    )

    op.execute(
        f"""
        CREATE FUNCTION enforce_provider_context_terminal_miss_provenance()
        RETURNS trigger AS $$
        BEGIN
            PERFORM 1
            FROM signals s
            JOIN messages m ON m.id=s.source_message_id
            WHERE s.id=NEW.signal_id
              AND m.id=NEW.message_id
              AND m.source_id=NEW.source_id
              AND s.source_posted_at=NEW.signal_posted_at;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'provider context terminal miss signal provenance invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_provider_context_terminal_miss_validate
        BEFORE INSERT ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION enforce_provider_context_terminal_miss_provenance();
        """
    )

    op.execute(
        f"""
        CREATE FUNCTION prevent_provider_context_terminal_miss_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'provider context terminal misses are immutable';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_provider_context_terminal_miss_immutable
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION prevent_provider_context_terminal_miss_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_provider_context_terminal_miss_immutable ON {TABLE}"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_provider_context_terminal_miss_mutation()")
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_provider_context_terminal_miss_validate ON {TABLE}"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_provider_context_terminal_miss_provenance()")
    op.drop_index("ix_provider_context_terminal_miss_source_posted", table_name=TABLE)
    op.drop_table(TABLE)
