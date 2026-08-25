"""Replace the stale Matthew alias with the current AJD source.

Revision ID: 0029_retire_stale_matthew
Revises: 0028_retire_tdc_provider
Create Date: 2026-08-25

The obsolete private channel is removed from selectable sources while its historical
evidence remains immutable. AJD TRADES is the current user-selected paper source.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_retire_stale_matthew"
down_revision: str | None = "0028_retire_tdc_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STALE_MATTHEW_CHAT_ID = -1003826850285


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE sources
        SET status='revoked', updated_at=now()
        WHERE chat_id={_STALE_MATTHEW_CHAT_ID}
        """
    )


def downgrade() -> None:
    # Restoring an obsolete provider could re-enable stale execution unexpectedly.
    pass
