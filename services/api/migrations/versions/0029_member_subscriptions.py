"""Add manual USDC subscription approval and renewal tracking.

Revision ID: 0029_member_subscriptions
Revises: 0028_retire_tdc_provider
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_member_subscriptions"
down_revision: str | None = "0028_retire_tdc_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscription_payment_claims",
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
        sa.Column("plan_code", sa.String(length=16), nullable=False),
        sa.Column("expected_amount_eur", sa.Integer(), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False, server_default="USDC"),
        sa.Column("network", sa.String(length=24), nullable=False, server_default="solana"),
        sa.Column("transaction_signature", sa.String(length=160), nullable=False, unique=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("plan_code IN ('monthly','annual')", name="ck_subscription_claim_plan"),
        sa.CheckConstraint("expected_amount_eur IN (99,999)", name="ck_subscription_claim_amount"),
        sa.CheckConstraint("asset = 'USDC'", name="ck_subscription_claim_asset"),
        sa.CheckConstraint("network = 'solana'", name="ck_subscription_claim_network"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected')",
            name="ck_subscription_claim_status",
        ),
    )
    op.create_index(
        "ix_subscription_claims_user_submitted",
        "subscription_payment_claims",
        ["user_id", "submitted_at"],
    )
    op.create_index(
        "ix_subscription_claims_status_submitted",
        "subscription_payment_claims",
        ["status", "submitted_at"],
    )

    op.create_table(
        "member_subscriptions",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("plan_code", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("active_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_payment_claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscription_payment_claims.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("plan_code IN ('monthly','annual')", name="ck_member_subscription_plan"),
        sa.CheckConstraint("status IN ('active','suspended')", name="ck_member_subscription_status"),
    )
    op.create_index(
        "ix_member_subscriptions_active_until",
        "member_subscriptions",
        ["active_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_member_subscriptions_active_until", table_name="member_subscriptions")
    op.drop_table("member_subscriptions")
    op.drop_index("ix_subscription_claims_status_submitted", table_name="subscription_payment_claims")
    op.drop_index("ix_subscription_claims_user_submitted", table_name="subscription_payment_claims")
    op.drop_table("subscription_payment_claims")
