"""Create the Super Signals core data model.

Revision ID: 0001_core_data_model
Revises:
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_core_data_model"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "roles",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.String(length=120)),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'invited'"),
        ),
        sa.Column("pin_hash", sa.String(length=255)),
        sa.Column(
            "two_factor_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "passkey_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'suspended', 'revoked')",
            name="ck_users_status",
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "user_roles",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "granted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "invitations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "role_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("roles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_invitations_single_terminal_state",
        ),
        sa.UniqueConstraint("key_hash", name="uq_invitations_key_hash"),
    )

    op.create_table(
        "telegram_accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("phone_number_e164", sa.String(length=24), nullable=False),
        sa.Column("session_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("session_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("last_connected_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'connected', 'disconnected', 'revoked')",
            name="ck_telegram_accounts_status",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "phone_number_e164",
            name="uq_telegram_accounts_owner_phone",
        ),
        sa.UniqueConstraint(
            "session_fingerprint",
            name="uq_telegram_accounts_session_fingerprint",
        ),
    )

    op.create_table(
        "sources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "telegram_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_title", sa.String(length=255)),
        sa.Column("source_alias", sa.String(length=120), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'testing'"),
        ),
        sa.Column(
            "redistribution_permission_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("permission_notes", sa.Text()),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('testing', 'paused', 'live', 'revoked')",
            name="ck_sources_status",
        ),
        sa.UniqueConstraint(
            "telegram_account_id",
            "chat_id",
            name="uq_sources_account_chat",
        ),
    )

    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column(
            "raw_payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column(
            "ingestion_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "ingestion_status IN ('received', 'parsed', 'skipped', 'duplicate', 'error')",
            name="ck_messages_ingestion_status",
        ),
        sa.UniqueConstraint(
            "source_id",
            "telegram_message_id",
            name="uq_messages_source_telegram_id",
        ),
    )

    op.create_table(
        "signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=40)),
        sa.Column("side", sa.String(length=4)),
        sa.Column(
            "order_type",
            sa.String(length=12),
            nullable=False,
            server_default=sa.text("'market'"),
        ),
        sa.Column("entry_low", sa.Numeric(precision=24, scale=10)),
        sa.Column("entry_high", sa.Numeric(precision=24, scale=10)),
        sa.Column("stop_loss", sa.Numeric(precision=24, scale=10)),
        sa.Column(
            "parser_status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'review'"),
        ),
        sa.Column("skip_reason", sa.Text()),
        sa.Column(
            "risk_multiplier",
            sa.Numeric(precision=8, scale=4),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("original_text", sa.Text(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_signals_side"),
        sa.CheckConstraint(
            "order_type IN ('market', 'pending')",
            name="ck_signals_order_type",
        ),
        sa.CheckConstraint(
            "parser_status IN ('accepted', 'skipped', 'review')",
            name="ck_signals_parser_status",
        ),
        sa.CheckConstraint("risk_multiplier > 0", name="ck_signals_risk_multiplier"),
        sa.CheckConstraint(
            "entry_high IS NULL OR entry_low IS NULL OR entry_high >= entry_low",
            name="ck_signals_entry_range",
        ),
        sa.UniqueConstraint("source_message_id", name="uq_signals_source_message_id"),
    )

    op.create_table(
        "positions",
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
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tp_index", sa.SmallInteger(), nullable=False),
        sa.Column("take_profit", sa.Numeric(precision=24, scale=10)),
        sa.Column(
            "planned_risk_percent",
            sa.Numeric(precision=5, scale=2),
            nullable=False,
        ),
        sa.Column("broker_position_id", sa.String(length=120)),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'planned'"),
        ),
        sa.Column("entry_price", sa.Numeric(precision=24, scale=10)),
        sa.Column("exit_price", sa.Numeric(precision=24, scale=10)),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("pnl_amount", sa.Numeric(precision=18, scale=2)),
        sa.Column("pnl_percent", sa.Numeric(precision=10, scale=4)),
        sa.Column("close_reason", sa.String(length=80)),
        *_timestamps(),
        sa.CheckConstraint("tp_index > 0", name="ck_positions_tp_index"),
        sa.CheckConstraint(
            "planned_risk_percent IN (0.5, 1.0, 2.0)",
            name="ck_positions_risk_percent",
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'open', 'closed', 'skipped', 'error')",
            name="ck_positions_status",
        ),
        sa.UniqueConstraint(
            "signal_id",
            "user_id",
            "tp_index",
            name="uq_positions_signal_user_tp",
        ),
    )
    op.create_index(
        "uq_positions_broker_position_id",
        "positions",
        ["broker_position_id"],
        unique=True,
        postgresql_where=sa.text("broker_position_id IS NOT NULL"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_audit_events_entity",
        "audit_events",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_audit_events_created_at",
        "audit_events",
        ["created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION prevent_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_update
        BEFORE UPDATE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_event_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_no_delete
        BEFORE DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_event_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_delete ON audit_events")
    op.execute("DROP TRIGGER IF EXISTS audit_events_no_update ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_event_mutation")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("uq_positions_broker_position_id", table_name="positions")
    op.drop_table("positions")
    op.drop_table("signals")
    op.drop_table("messages")
    op.drop_table("sources")
    op.drop_table("telegram_accounts")
    op.drop_table("invitations")
    op.drop_table("user_roles")
    op.drop_table("users")
    op.drop_table("roles")
