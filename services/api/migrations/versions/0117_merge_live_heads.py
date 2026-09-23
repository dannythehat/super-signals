"""Merge the two live migration heads created on 23 September 2026.

Revision ID: 0117_merge_live_heads
Revises: 0116_shadow_trade_global, 0114_fxtradingvision_demo_on
Create Date: 2026-09-23
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0117_merge_live_heads"
down_revision: tuple[str, str] = (
    "0116_shadow_trade_global",
    "0114_fxtradingvision_demo_on",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
