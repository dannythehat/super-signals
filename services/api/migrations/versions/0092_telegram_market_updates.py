"""Compatibility bridge for the provider scoreboard repair.

Revision ID: 0092_telegram_market_updates
Revises: 0087_provider_scoreboard
Create Date: 2026-09-16

PR #179 was originally prepared against a local migration label that was never
shipped to the production branch. Production and the deployed database both stop
at ``0087_provider_scoreboard``. Keep the already-reviewed 0093 migration's
predecessor stable by inserting this explicit no-op bridge.

This migration intentionally makes no schema or data changes.
"""

from collections.abc import Sequence

revision: str = "0092_telegram_market_updates"
down_revision: str | None = "0087_provider_scoreboard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
