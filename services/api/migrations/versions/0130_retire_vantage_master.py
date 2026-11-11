"""Remove the retired Vantage master from active application account selection.

The verified Super Signals execution source is local MT5 master 700207861.
Historical account rows, credentials and broker deals remain intact. This
migration changes no broker positions and sends no broker requests.
"""
from __future__ import annotations

from alembic import op

revision: str = '0130_retire_vantage'
down_revision: str | None = '0129_owner_feed'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE mt5_accounts
        SET status='revoked', last_error_code='retired_master_25911913',
            updated_at=CURRENT_TIMESTAMP
        WHERE broker='vantage' AND account_environment='demo'
          AND login='25911913' AND server='VantageMarkets-Demo'
          AND status<>'revoked'
    """)
    op.execute("""
        UPDATE mt5_account_profiles
        SET status='revoked', is_active=false,
            last_error_code='retired_master_25911913', updated_at=CURRENT_TIMESTAMP
        WHERE broker='vantage' AND account_environment='demo'
          AND login='25911913' AND server='VantageMarkets-Demo'
          AND (status<>'revoked' OR is_active=true)
    """)


def downgrade() -> None:
    # Rolling back software must never reactivate a deliberately retired source.
    pass
