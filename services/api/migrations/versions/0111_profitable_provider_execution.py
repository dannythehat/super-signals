"""Restore the profitable provider basket to owner forward execution.

Revision ID: 0111_profitable_exec
Revises: 0110_aidy_gold_view_scores
Create Date: 2026-09-22

Policy:
* A currently profitable benchmark provider that is only in Shadow becomes Testing.
* A Testing provider with a non-positive benchmark result is returned to Shadow.
* Newly promoted providers are put on probation so member LIVE accounts remain excluded
  until separately graduated. Owner/demo forward testing is both BUY and SELL; the
  canonical probation gate implements that behaviour.

Paused/revoked sources are never re-enabled here.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0111_profitable_exec"
down_revision: str | None = "0110_aidy_gold_view_scores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOTE = (
    "0111 profitable-provider restore: owner/demo forward test on both BUY and SELL; "
    "member LIVE remains excluded until explicit graduation."
)


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO provider_execution_probation (
            source_id, enabled_at, enabled_note, graduated, graduated_at, created_at, updated_at
        )
        SELECT s.id, now(), '{_NOTE}', false, NULL, now(), now()
        FROM sources AS s
        JOIN provider_benchmark_performance AS p ON p.source_id = s.id
        WHERE p.benchmark_pnl_usd > 0
          AND s.status = 'shadow'
        ON CONFLICT (source_id) DO NOTHING
        """
    )

    op.execute(
        """
        UPDATE sources AS s
        SET status = 'testing', updated_at = now()
        FROM provider_benchmark_performance AS p
        WHERE p.source_id = s.id
          AND p.benchmark_pnl_usd > 0
          AND s.status = 'shadow'
        """
    )

    op.execute(
        """
        UPDATE sources AS s
        SET status = 'shadow', updated_at = now()
        FROM provider_benchmark_performance AS p
        WHERE p.source_id = s.id
          AND p.benchmark_pnl_usd <= 0
          AND s.status = 'testing'
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE sources AS s
        SET status = 'shadow', updated_at = now()
        FROM provider_execution_probation AS pep
        WHERE pep.source_id = s.id
          AND pep.enabled_note = '{_NOTE}'
          AND s.status = 'testing'
        """
    )
    op.execute(
        f"""
        DELETE FROM provider_execution_probation
        WHERE enabled_note = '{_NOTE}'
          AND graduated = false
        """
    )
