"""Revalidate existing scoreable Provider Lab outcomes against Gate 0.

Revision ID: 0071_provider_gate0_existing
Revises: 0070_provider_profile_gate0
Create Date: 2026-09-09

Existing scoreable outcomes must meet the same forward-only profile standard as new
ones.  The immutable profile version attached to the historical signal is the only
profile considered; current/future provider learning is deliberately ignored.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0071_provider_gate0_existing"
down_revision: str | None = "0070_provider_profile_gate0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE shadow_trades t
        SET score_eligible=false,
            score_exclusion_reason='provider_profile_gate_incomplete',
            aidy_score_blocked=true,
            pnl_percent=NULL,
            updated_at=now()
        WHERE t.score_eligible
          AND NOT EXISTS (
              SELECT 1
              FROM provider_research_profile_versions v
              WHERE v.id=t.provider_profile_version_id
                AND v.source_id=t.source_id
                AND v.effective_at<=t.signal_posted_at
                AND provider_profile_gate0_snapshot_qualified(v.profile_snapshot)
          )
        """
    )


def downgrade() -> None:
    # Fail-safe downgrade: do not manufacture score eligibility after it has been
    # removed. Restoring a historical score would require reconstructing the exact
    # pre-Gate-0 decision, so downgrade intentionally leaves these rows excluded.
    pass
