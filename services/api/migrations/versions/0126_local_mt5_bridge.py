"""Durable command queue for the outbound-polling local MT5 bridge.

Revision ID: 0126_local_mt5_bridge
Revises: 0125_incident_20261001_override
Create Date: 2026-10-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0126_local_mt5_bridge"
down_revision: str | None = "0125_incident_20261001_override"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_bridge_workers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("profile", sa.String(length=80), nullable=False),
        sa.Column("version", sa.String(length=40), nullable=False),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("worker_id", name="uq_local_bridge_workers_worker_id"),
    )
    op.create_index(
        "ix_local_bridge_workers_profile_seen",
        "local_bridge_workers",
        ["profile", "last_seen_at"],
    )

    op.create_table(
        "local_bridge_commands",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("profile", sa.String(length=80), nullable=False),
        sa.Column("account_id", sa.String(length=120), nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("claimed_by", sa.String(length=100), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued','claimed','succeeded','failed','ambiguous')",
            name="ck_local_bridge_commands_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_local_bridge_commands_idempotency_key"),
    )
    op.create_index(
        "ix_local_bridge_commands_claim",
        "local_bridge_commands",
        ["profile", "status", "created_at"],
    )
    op.create_index(
        "ix_local_bridge_commands_lease",
        "local_bridge_commands",
        ["claimed_by", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_local_bridge_commands_lease", table_name="local_bridge_commands")
    op.drop_index("ix_local_bridge_commands_claim", table_name="local_bridge_commands")
    op.drop_table("local_bridge_commands")
    op.drop_index("ix_local_bridge_workers_profile_seen", table_name="local_bridge_workers")
    op.drop_table("local_bridge_workers")
