"""Persist immutable per-signal Provider Intelligence context provenance.

Revision ID: 0058_provider_aidy_context
Revises: 0057_provider_pit_boundary
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058_provider_aidy_context"
down_revision: str | None = "0057_provider_pit_boundary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_signal_context_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signal_posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_profile_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_profile_version_no", sa.Integer(), nullable=False),
        sa.Column("provider_profile_effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aidy_requested_as_of_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aidy_context_as_of_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aidy_context_lag_seconds", sa.Integer(), nullable=False),
        sa.Column("aidy_context_hash", sa.String(length=64), nullable=False),
        sa.Column("aidy_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("aidy_snapshot_digest", sa.String(length=64), nullable=False),
        sa.Column("aidy_snapshot_archive_key", sa.Text(), nullable=False),
        sa.Column("session_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("regime_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("data_quality_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("market_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("provenance_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attachment_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attachment_digest", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.String(length=48), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["provider_profile_version_id"],
            ["provider_research_profile_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("signal_id", name="uq_provider_signal_context_attachment_signal"),
        sa.CheckConstraint(
            "provider_profile_version_no > 0",
            name="ck_provider_context_profile_version_positive",
        ),
        sa.CheckConstraint(
            "provider_profile_effective_at <= signal_posted_at",
            name="ck_provider_context_profile_not_future",
        ),
        sa.CheckConstraint(
            "aidy_requested_as_of_utc = signal_posted_at",
            name="ck_provider_context_requested_matches_signal",
        ),
        sa.CheckConstraint(
            "aidy_context_as_of_utc <= signal_posted_at",
            name="ck_provider_context_aidy_not_future",
        ),
        sa.CheckConstraint(
            "aidy_context_lag_seconds >= 0 AND aidy_context_lag_seconds <= 600",
            name="ck_provider_context_lag_bounded",
        ),
        sa.CheckConstraint(
            "aidy_snapshot_digest ~ '^[0-9a-f]{64}$'",
            name="ck_provider_context_snapshot_digest_hex",
        ),
        sa.CheckConstraint(
            "attachment_digest ~ '^[0-9a-f]{64}$'",
            name="ck_provider_context_attachment_digest_hex",
        ),
        sa.CheckConstraint(
            "COALESCE((provenance_json->>'private_forward_only')::boolean,false) = true",
            name="ck_provider_context_private_forward_only",
        ),
        sa.CheckConstraint(
            "COALESCE((provenance_json->>'live_money_execution_allowed')::boolean,true) = false",
            name="ck_provider_context_no_live_money",
        ),
    )
    op.create_index(
        "ix_provider_signal_context_source_posted",
        "provider_signal_context_attachments",
        ["source_id", "signal_posted_at"],
    )
    op.create_index(
        "ix_provider_signal_context_snapshot",
        "provider_signal_context_attachments",
        ["aidy_snapshot_id"],
    )

    op.execute(
        """
        CREATE FUNCTION enforce_provider_signal_context_attachment()
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
                RAISE EXCEPTION 'provider context signal provenance invalid';
            END IF;

            PERFORM 1
            FROM provider_research_profile_versions v
            WHERE v.id=NEW.provider_profile_version_id
              AND v.source_id=NEW.source_id
              AND v.version_no=NEW.provider_profile_version_no
              AND v.effective_at=NEW.provider_profile_effective_at
              AND v.effective_at <= NEW.signal_posted_at;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'provider context profile provenance invalid';
            END IF;

            PERFORM 1
            FROM shadow_trades t
            WHERE t.signal_id=NEW.signal_id
              AND t.source_id=NEW.source_id
              AND t.provider_profile_pit_status='resolved'
              AND t.provider_profile_version_id=NEW.provider_profile_version_id
              AND t.provider_profile_version_no=NEW.provider_profile_version_no
              AND t.provider_profile_effective_at=NEW.provider_profile_effective_at;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'provider context requires resolved shadow research anchor';
            END IF;

            IF NEW.aidy_requested_as_of_utc <> NEW.signal_posted_at
               OR NEW.aidy_context_as_of_utc > NEW.signal_posted_at
               OR NEW.provider_profile_effective_at > NEW.signal_posted_at THEN
                RAISE EXCEPTION 'provider context point-in-time boundary invalid';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_signal_context_attachment_validate
        BEFORE INSERT ON provider_signal_context_attachments
        FOR EACH ROW EXECUTE FUNCTION enforce_provider_signal_context_attachment()
        """
    )

    op.execute(
        """
        CREATE FUNCTION prevent_provider_signal_context_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'provider signal context attachments are immutable';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_provider_signal_context_attachment_immutable
        BEFORE UPDATE OR DELETE ON provider_signal_context_attachments
        FOR EACH ROW EXECUTE FUNCTION prevent_provider_signal_context_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_provider_signal_context_attachment_immutable "
        "ON provider_signal_context_attachments"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_provider_signal_context_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_provider_signal_context_attachment_validate "
        "ON provider_signal_context_attachments"
    )
    op.execute("DROP FUNCTION IF EXISTS enforce_provider_signal_context_attachment()")
    op.drop_index("ix_provider_signal_context_snapshot", table_name="provider_signal_context_attachments")
    op.drop_index(
        "ix_provider_signal_context_source_posted",
        table_name="provider_signal_context_attachments",
    )
    op.drop_table("provider_signal_context_attachments")
