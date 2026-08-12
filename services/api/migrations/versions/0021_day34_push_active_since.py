"""Add push subscription activation boundary.

Revision ID: 0021_day34_push_active_since
Revises: 0020_day34_summary_state
Create Date: 2026-08-12

A device should receive notifications created after it opted in, not a backlog of every
historical notification stored before subscription. ``active_since`` is reset only when
a disabled subscription is explicitly re-enabled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_day34_push_active_since"
down_revision: str | None = "0020_day34_summary_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "push_subscriptions",
        sa.Column(
            "active_since",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("push_subscriptions", "active_since")
