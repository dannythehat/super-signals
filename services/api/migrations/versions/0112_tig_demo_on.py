"""Enable TIG alongside all profitable providers on the owner demo path.

Revision ID: 0112_tig_demo_on
Revises: 0111_profitable_exec
Create Date: 2026-09-22

The owner account is demo. Sources in testing execute both BUY and SELL on the owner
reference/demo account. Active probation continues to exclude member LIVE accounts.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0112_tig_demo_on"
down_revision: str | None = "0111_profitable_exec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE sources AS s
        SET status = 'testing', updated_at = now()
        FROM provider_benchmark_performance AS p
        WHERE p.source_id = s.id
          AND p.benchmark_pnl_usd > 0
          AND s.status <> 'revoked'
        """
    )
    op.execute(
        """
        UPDATE sources
        SET status = 'testing', updated_at = now()
        WHERE chat_title = 'TIG’s Asia Trades'
          AND status <> 'revoked'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE sources
        SET status = 'shadow', updated_at = now()
        WHERE chat_title = 'TIG’s Asia Trades'
          AND status = 'testing'
        """
    )
