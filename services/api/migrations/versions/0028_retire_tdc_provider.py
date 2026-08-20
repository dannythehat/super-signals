"""Permanently retire TDC from Super Signals execution.

Revision ID: 0028_retire_tdc_provider
Revises: 0027_gtmo_canonical_feed
Create Date: 2026-08-20

TDC is no longer an eligible provider. Revoked sources are not listened to and the
canonical dispatcher only executes sources whose current state is testing or live.
Historical raw provider evidence remains available for internal forensics only.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_retire_tdc_provider"
down_revision: str | None = "0027_gtmo_canonical_feed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TDC_CHAT_ID = -1004415242875


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE sources
        SET status='revoked', updated_at=now()
        WHERE chat_id={_TDC_CHAT_ID}
        """
    )


def downgrade() -> None:
    # Re-enabling a retired provider must be an explicit product decision. A schema
    # downgrade must never silently make TDC executable again.
    pass
