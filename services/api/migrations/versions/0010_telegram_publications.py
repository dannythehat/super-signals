"""Add publication records for the private Telegram mirror."""

from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_telegram_publications"
down_revision: str | None = "0009_signal_events_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("signal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("signals.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("publication_kind", sa.String(length=40), nullable=False, server_default="signal_created"),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
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
        sa.CheckConstraint("status IN ('pending', 'sending', 'sent', 'failed', 'suppressed')", name="ck_telegram_publications_status"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_telegram_publications_attempt_count"),
        sa.UniqueConstraint("signal_id", "publication_kind", name="uq_telegram_publications_signal_kind"),
    )
    op.create_index("ix_telegram_publications_status_created", "telegram_publications", ["status", "created_at"])
    op.execute("""
        INSERT INTO telegram_publications (signal_id, publication_kind, status, failure_code, failure_reason)
        SELECT id, 'signal_created', 'suppressed', 'pre_day19_history', 'Existing event predates Day 19.'
        FROM signals
        ON CONFLICT (signal_id, publication_kind) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_index("ix_telegram_publications_status_created", table_name="telegram_publications")
    op.drop_table("telegram_publications")
