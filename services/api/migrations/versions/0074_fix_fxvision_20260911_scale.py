"""Correct FXTradingVision 2026-09-11 reviewed outcome cash and pip scaling."""
from collections.abc import Sequence
from alembic import op

revision: str = "0074_fix_fxvision_scale"
down_revision: str = "0073_fxvision_20260911"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

USER_ID = "ea604df2-f8ee-47d1-bc51-f0078dbf160d"
REASON = "reviewed_provider_result_2026_09_11"
BASE_CAPITAL = 1517.23


def upgrade() -> None:
    op.execute(f"""
        UPDATE positions
        SET pnl_amount = pnl_amount / 100.0,
            pnl_percent = (pnl_amount / 100.0) / {BASE_CAPITAL} * 100.0,
            updated_at = now()
        WHERE user_id=UUID '{USER_ID}' AND close_reason='{REASON}';
    """)
    op.execute(f"""
        UPDATE performance_trade_outcomes o
        SET cash_pnl = p.pnl_amount,
            return_percent = p.pnl_percent,
            net_pips = o.net_pips / 10.0,
            pip_size = 0.1,
            derived_at = now()
        FROM positions p
        WHERE o.position_id=p.id
          AND o.user_id=UUID '{USER_ID}'
          AND o.close_reason='{REASON}';
    """)


def downgrade() -> None:
    op.execute(f"""
        UPDATE performance_trade_outcomes o
        SET cash_pnl = p.pnl_amount * 100.0,
            return_percent = p.pnl_percent * 100.0,
            net_pips = o.net_pips * 10.0,
            derived_at = now()
        FROM positions p
        WHERE o.position_id=p.id
          AND o.user_id=UUID '{USER_ID}'
          AND o.close_reason='{REASON}';
        UPDATE positions
        SET pnl_amount = pnl_amount * 100.0,
            pnl_percent = pnl_percent * 100.0,
            updated_at = now()
        WHERE user_id=UUID '{USER_ID}' AND close_reason='{REASON}';
    """)
