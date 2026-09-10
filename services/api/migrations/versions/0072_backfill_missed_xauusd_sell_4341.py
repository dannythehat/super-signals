"""Backfill the missed 2026-09-10 XAUUSD SELL 4341 three-target winner.

This migration is intentionally left as a data-backfill placeholder pending the
production trade ledger identifiers. It must not fabricate provider/account IDs.
"""

from alembic import op

revision = "0072_backfill_missed_xauusd_sell_4341"
down_revision = "0071_merge_one_percent_reporting_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The authoritative trade/provider/account identifiers must be resolved from
    # production before inserting the correction. No guessed IDs are permitted.
    pass


def downgrade() -> None:
    pass
