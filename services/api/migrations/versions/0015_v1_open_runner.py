"""Add V1 open-runner marker to canonical signals.

Revision ID: 0015_v1_open_runner
Revises: 0014_day26_position_execution
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_v1_open_runner"
down_revision: str | None = "0014_day26_position_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "signals",
        sa.Column(
            "has_open_runner",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("signals", "has_open_runner")
