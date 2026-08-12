"""Add Day 34 summary publication cutover state.

Revision ID: 0020_day34_summary_state
Revises: 0019_day34_notifications
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_day34_summary_state"
down_revision: str | None = "0019_day34_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "day34_summary_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column(
            "publish_after",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_seeded_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_day34_summary_state_singleton"),
    )
    op.execute("INSERT INTO day34_summary_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("day34_summary_state")
