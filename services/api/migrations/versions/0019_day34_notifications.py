"""Add Day 34 notifications, push delivery and pinned live-board state.

Revision ID: 0019_day34_notifications
Revises: 0018_day33_performance_ledger
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_day34_notifications"
down_revision: str | None = "0018_day33_performance_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_live_board_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("destination_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("rendered_text", sa.Text(), nullable=True),
        sa.Column("source_digest", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="uninitialized"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_telegram_live_board_singleton"),
        sa.CheckConstraint(
            "status IN ('uninitialized','sending','ready','failed')",
            name="ck_telegram_live_board_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_telegram_live_board_attempt_count"),
    )
    op.execute("INSERT INTO telegram_live_board_state (id) VALUES (1)")

    op.create_table(
        "notification_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("event_key", sa.String(length=200), nullable=False, unique=True),
        sa.Column(
            "signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("signals.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "lifecycle_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("signal_lifecycle_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("audience", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("audience IN ('shared','user')", name="ck_notification_events_audience"),
        sa.CheckConstraint("length(trim(event_key)) > 0", name="ck_notification_events_key_nonempty"),
    )
    op.create_index(
        "ix_notification_events_user_created",
        "notification_events",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_notification_events_signal_created",
        "notification_events",
        ["signal_id", "created_at"],
    )
    op.execute("ALTER TABLE notification_events ENABLE ROW LEVEL SECURITY")

    op.create_table(
        "notification_reads",
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notification_events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("ALTER TABLE notification_reads ENABLE ROW LEVEL SECURITY")

    op.create_table(
        "telegram_notification_deliveries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notification_events.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("rendered_text", sa.Text(), nullable=True),
        sa.Column("destination_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','failed','suppressed')",
            name="ck_telegram_notification_deliveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_telegram_notification_deliveries_attempt_count",
        ),
    )
    op.create_index(
        "ix_telegram_notification_deliveries_status_created",
        "telegram_notification_deliveries",
        ["status", "created_at"],
    )

    op.create_table(
        "push_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("endpoint_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("p256dh", sa.Text(), nullable=False),
        sa.Column("auth", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("failure_count >= 0", name="ck_push_subscriptions_failure_count"),
    )
    op.create_index(
        "ix_push_subscriptions_user_enabled",
        "push_subscriptions",
        ["user_id", "enabled"],
    )
    op.execute("ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY")

    op.create_table(
        "push_notification_deliveries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notification_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("push_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending','sending','sent','failed','suppressed')",
            name="ck_push_notification_deliveries_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_push_deliveries_attempt_count"),
        sa.UniqueConstraint(
            "notification_id",
            "subscription_id",
            name="uq_push_notification_delivery_event_subscription",
        ),
    )
    op.create_index(
        "ix_push_notification_deliveries_status_created",
        "push_notification_deliveries",
        ["status", "created_at"],
    )
    op.execute("ALTER TABLE push_notification_deliveries ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(
        "ix_push_notification_deliveries_status_created",
        table_name="push_notification_deliveries",
    )
    op.drop_table("push_notification_deliveries")
    op.drop_index("ix_push_subscriptions_user_enabled", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
    op.drop_index(
        "ix_telegram_notification_deliveries_status_created",
        table_name="telegram_notification_deliveries",
    )
    op.drop_table("telegram_notification_deliveries")
    op.drop_table("notification_reads")
    op.drop_index("ix_notification_events_signal_created", table_name="notification_events")
    op.drop_index("ix_notification_events_user_created", table_name="notification_events")
    op.drop_table("notification_events")
    op.drop_table("telegram_live_board_state")
