"""Merge the one-percent reporting migration into the canonical migration line.

Revision ID: 0071_merge_one_percent_reporting_heads
Revises: 0070_one_percent_per_tp_model, 0069_provider_day20_mgmt
Create Date: 2026-09-10

The branch previously contained two duplicate 0070 reviewed-provider migrations and
one legitimate 0070 one-percent reporting migration. The duplicate reviewed-provider
migrations have been removed; this merge closes the remaining migration fork without
changing production data.
"""

from collections.abc import Sequence

revision: str = "0071_merge_one_percent_reporting_heads"
down_revision: tuple[str, str] = (
    "0070_one_percent_per_tp_model",
    "0069_provider_day20_mgmt",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
