"""Allow terminal recording when no historical AIDY PIT context exists.

A 404 no_pit_context for an immutable historical signal is not retryable: a later retry
cannot create evidence that did not exist at that signal timestamp without rewriting
history. Persist it once, like pit_context_stale, so the context backlog can drain safely.

Revision ID: 0101_aidy_no_pit_terminal
Revises: 0100_aidy_context_all_signals
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0101_aidy_no_pit_terminal"
down_revision: str | None = "0100_aidy_context_all_signals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE provider_signal_context_terminal_misses
          DROP CONSTRAINT IF EXISTS ck_provider_context_terminal_miss_reason
        """
    )
    op.execute(
        """
        ALTER TABLE provider_signal_context_terminal_misses
          ADD CONSTRAINT ck_provider_context_terminal_miss_reason
          CHECK (reason IN ('pit_context_stale','no_pit_context'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE provider_signal_context_terminal_misses
          DROP CONSTRAINT IF EXISTS ck_provider_context_terminal_miss_reason
        """
    )
    op.execute(
        """
        ALTER TABLE provider_signal_context_terminal_misses
          ADD CONSTRAINT ck_provider_context_terminal_miss_reason
          CHECK (reason = 'pit_context_stale')
        """
    )
