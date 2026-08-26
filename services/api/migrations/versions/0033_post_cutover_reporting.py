"""Limit the incident override to outcomes closed before the production fix.

Revision ID: 0033_post_cutover_reporting
Revises: 0032_merge_reporting_override
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_post_cutover_reporting"
down_revision: str | None = "0032_merge_reporting_override"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FIX_LIVE_AT = "2026-08-26 14:46:01.889520+00"


def upgrade() -> None:
    op.add_column(
        "performance_reporting_overrides",
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        f"""
        UPDATE performance_reporting_overrides
        SET cutoff_at=TIMESTAMPTZ '{_FIX_LIVE_AT}', updated_at=now()
        WHERE incident_key='incident-2026-08-26-trade-management'
        """
    )
    op.alter_column(
        "performance_reporting_overrides",
        "cutoff_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("performance_reporting_overrides", "cutoff_at")
