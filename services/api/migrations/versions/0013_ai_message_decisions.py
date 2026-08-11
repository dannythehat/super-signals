"""Add AI message decision evidence.

Revision ID: 0013_ai_message_decisions
Revises: 0012_mt5_accounts
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_ai_message_decisions"
down_revision: str | None = "0012_mt5_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_message_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_index", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("decision", sa.String(length=30), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("extracted", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("response_id", sa.String(length=160)),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("decision_source", sa.String(length=30), nullable=False),
        sa.Column("raw_text_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", "revision_index", name="uq_ai_message_decisions_message_revision"),
    )
    op.create_index("ix_ai_message_decisions_created", "ai_message_decisions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_message_decisions_created", table_name="ai_message_decisions")
    op.drop_table("ai_message_decisions")
