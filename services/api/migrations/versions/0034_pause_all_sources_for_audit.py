"""Pause every executable provider while the provider audit is completed.

Revision ID: 0034_pause_all_sources_for_audit
Revises: 0033_post_cutover_reporting
Create Date: 2026-08-27

This is an intentional operational freeze. It changes only source execution status;
provider records, messages, signals and performance evidence are preserved for audit.
Revoked sources remain revoked. Re-enabling providers after the audit is deliberately
manual/provider-specific, so downgrade does not guess their former statuses.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034_pause_all_sources_for_audit"
down_revision: str | None = "0033_post_cutover_reporting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE sources
        SET status='paused', updated_at=now()
        WHERE status IN ('testing','shadow','live')
        """
    )


def downgrade() -> None:
    # Intentionally irreversible: the pre-audit mix of testing/shadow/live providers
    # must not be restored automatically. Providers are re-enabled one-by-one only
    # after their new audited policy is approved.
    pass
