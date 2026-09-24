"""Allow durable post-execution edit observations.

Revision ID: 0124_post_execution_edit_observation
Revises: 0123_rebase_telegram_financials
Create Date: 2026-09-24

The canonical AI recovery path records fresh edits to an already-executed signal as
post_execution_edit evidence. Production still had the original Day 9 check constraint
that allowed only canonical/duplicate, causing recovery of provider edits to fail before
the decision could be stored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0124_post_execution_edit_observation"
down_revision: str | None = "0123_rebase_telegram_financials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_signal_observations_disposition",
        "signal_observations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_signal_observations_disposition",
        "signal_observations",
        "disposition IN ('canonical','duplicate','post_execution_edit')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE signal_observations
            SET disposition='duplicate'
            WHERE disposition='post_execution_edit'
            """
        )
    )
    op.drop_constraint(
        "ck_signal_observations_disposition",
        "signal_observations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_signal_observations_disposition",
        "signal_observations",
        "disposition IN ('canonical','duplicate')",
    )
