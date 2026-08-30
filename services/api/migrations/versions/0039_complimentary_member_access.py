"""Add complimentary access grants and one-time owner approval tokens.

Revision ID: 0039_complimentary_member_access
Revises: 0038_ranked_provider_activation
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039_complimentary_member_access"
down_revision: str | None = "0038_ranked_provider_activation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "complimentary_access_grants",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "granted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(length=40), nullable=False, server_default="owner_email"),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active','revoked')", name="ck_complimentary_access_status"),
    )

    op.create_table(
        "complimentary_access_tokens",
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
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_complimentary_access_tokens_user_created",
        "complimentary_access_tokens",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_complimentary_access_tokens_expiry",
        "complimentary_access_tokens",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_complimentary_access_tokens_expiry", table_name="complimentary_access_tokens")
    op.drop_index("ix_complimentary_access_tokens_user_created", table_name="complimentary_access_tokens")
    op.drop_table("complimentary_access_tokens")
    op.drop_table("complimentary_access_grants")
