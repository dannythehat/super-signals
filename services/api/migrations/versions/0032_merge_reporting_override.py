"""Merge member trade-number and reporting-override migration heads.

Revision ID: 0032_merge_reporting_override
Revises: 0031_member_trade_numbers, 0029_reporting_overrides
Create Date: 2026-08-26
"""

from collections.abc import Sequence

revision: str = "0032_merge_reporting_override"
down_revision: tuple[str, str] = (
    "0031_member_trade_numbers",
    "0029_reporting_overrides",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
