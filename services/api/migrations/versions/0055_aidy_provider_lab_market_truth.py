"""Use AIDY M1 as canonical Provider Lab market truth for non-scalpers.

Revision ID: 0055_aidy_provider_lab_truth
Revises: 0054_pause_member_access
Create Date: 2026-09-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0055_aidy_provider_lab_truth"
down_revision: str | None = "0054_pause_member_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_shadow_fair_quote_mode", "shadow_trades", type_="check")
    op.create_check_constraint(
        "ck_shadow_fair_quote_mode",
        "shadow_trades",
        "quote_mode IN ('unobserved','snapshot_poll','stream_quote','stream_tick','aidy_m1')",
    )
    op.execute(
        """
        UPDATE shadow_trades
        SET score_eligible=false,
            score_exclusion_reason='unsupported_style_scalper',
            updated_at=now()
        WHERE provider_style='scalper'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE shadow_trades
        SET quote_mode='unobserved',
            score_eligible=false,
            score_exclusion_reason='market_data_not_observed',
            updated_at=now()
        WHERE quote_mode='aidy_m1'
        """
    )
    op.drop_constraint("ck_shadow_fair_quote_mode", "shadow_trades", type_="check")
    op.create_check_constraint(
        "ck_shadow_fair_quote_mode",
        "shadow_trades",
        "quote_mode IN ('unobserved','snapshot_poll','stream_quote','stream_tick')",
    )
