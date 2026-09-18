"""Split the fingerprint's combined side+session sample floor into two independent flags.

Investigating why GOLDHUNTER -- a provider with a verified, well-evidenced track record
(43 resolved trades, both BUY and SELL individually well past the cohort floor, ~79% win
rate on each) -- had never had a single paper trade dispatched turned up the actual cause:
``provider_trade_fingerprints.cohort_sample_met`` required *both* a statistically
adequate side split *and* a statistically adequate session split before it would ever be
true, but ``provider_execution_probation.check_probation_eligibility`` only ever needs the
side answer to decide whether a signal's side matches this provider's best-evidenced side.
GOLDHUNTER's trades happen to cluster into one dominant session (London), so the session
half of that AND could never clear its own floor -- silently blocking every one of her
signals from ever reaching the paper broker, independent of how solid her side data was.

This adds ``side_sample_met`` and ``session_sample_met`` as their own columns so execution
eligibility can depend on side adequacy alone, while the fingerprint summary still reports
session insight separately once that mix has enough spread to say anything about it.
Existing (already-computed) rows default to false for both -- they are historical, append-
only records the table's trigger will not allow updating in place, and the fingerprint
runner recomputes a fresh row for every provider on its regular schedule, so the next pass
naturally repopulates the correct values without needing a backfill.

Revision ID: 0094_fingerprint_side_session_split
Revises: 0093_switch_on_profitable
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0094_fp_side_session_split"
down_revision: str | None = "0093_switch_on_profitable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE provider_trade_fingerprints "
        "ADD COLUMN side_sample_met boolean NOT NULL DEFAULT false, "
        "ADD COLUMN session_sample_met boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE provider_trade_fingerprints "
        "DROP COLUMN IF EXISTS side_sample_met, "
        "DROP COLUMN IF EXISTS session_sample_met"
    )
