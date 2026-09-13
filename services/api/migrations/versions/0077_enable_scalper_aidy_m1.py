"""Move PIT-resolved scalper research rows into deterministic AIDY M1 replay.

This is research-only. It does not alter live provider allocations, broker execution,
or the Super Signals 1% live-risk configuration. Existing ambiguity and revoked-source
exclusions remain fail-closed.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0077_enable_scalper_aidy_m1"
down_revision: str = "0076_revoke_stale_no_signal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only reopen the old blanket scalper exclusion. Rows excluded for any other
    # reason (legacy PIT, revoked provider, missing truth, explicit ambiguity, etc.)
    # are deliberately untouched.
    op.execute(
        """
        UPDATE shadow_trades
        SET score_eligible = false,
            score_exclusion_reason = 'outcome_pending_aidy_m1',
            aidy_score_blocked = false,
            updated_at = now()
        WHERE provider_style = 'scalper'
          AND score_exclusion_reason = 'unsupported_style_scalper'
          AND NOT COALESCE(aidy_terminal, false)
          AND provider_profile_pit_status = 'resolved'
          AND aidy_original_geometry IS NOT NULL;
        """
    )


def downgrade() -> None:
    # Do not destroy replay evidence or reclassify already-resolved outcomes.
    pass
