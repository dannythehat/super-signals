"""Add database-backed authentication rate limits.

Revision ID: 0022_day39_auth_rate_limits
Revises: 0021_day34_push_active_since
Create Date: 2026-08-13

Only privacy hashes are stored. No raw email, IP address, password or session token
belongs in the throttle table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_day39_auth_rate_limits"
down_revision: str | None = "0021_day34_push_active_since"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_rate_limits",
        sa.Column("scope", sa.String(length=40), primary_key=True),
        sa.Column("subject_hash", sa.String(length=64), primary_key=True),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_auth_rate_limits_attempt_count"),
    )
    op.create_index(
        "ix_auth_rate_limits_blocked_until",
        "auth_rate_limits",
        ["blocked_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_rate_limits_blocked_until", table_name="auth_rate_limits")
    op.drop_table("auth_rate_limits")
