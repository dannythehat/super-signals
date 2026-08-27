"""Allow approved provider-specific 5% position risk.

Revision ID: 0036_allow_provider_five_percent_positions
Revises: 0035_enable_core_paper_providers
Create Date: 2026-08-27

The execution policy now permits TIG SELL TP1/TP2 at 5% each, but the legacy
positions table constraint still capped planned_risk_percent at 4%. This migration
aligns the persistence constraint with the approved provider-policy ceiling. General
user risk validation remains unchanged; this only removes the stale database veto.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036_allow_provider_five_percent_positions"
down_revision: str | None = "0035_enable_core_paper_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_positions_risk_percent", "positions", type_="check")
    op.create_check_constraint(
        "ck_positions_risk_percent",
        "positions",
        "planned_risk_percent > 0 AND planned_risk_percent <= 5.0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_positions_risk_percent", "positions", type_="check")
    op.create_check_constraint(
        "ck_positions_risk_percent",
        "positions",
        "planned_risk_percent > 0 AND planned_risk_percent <= 4.0",
    )
