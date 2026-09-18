"""Track whether each AIDY reasoning annotation actually had live market context.

The reasoning engine (aidy_reasoning_engine.py) previously read only a signal's own
entry/stop/TP geometry plus the posting provider's own resolved-history fingerprint --
its own system prompt explicitly forbade commenting on market conditions it was never
given. This wires in AIDY's existing, already-PIT-safe market/regime context (the same
snapshot Day 10's provider_signal_context_attachments records, fetched fresh per signal
here via AidyContextClient) so the model can weigh whether a signal's direction runs
with or against the current multi-timeframe trend, session and event timing.

A signal reasoned long after it posted has no live context left to fetch -- the context
API only serves a bounded recent window, exactly the same reason
provider_signal_context_attachments already carries hundreds of permanent
"pit_context_stale" terminal misses for old signals. That is expected and must never
block reasoning about the signal's own geometry: this column records, per annotation,
whether market context was actually available and used, so a later analysis of
aidy_reasoning_annotations can honestly separate "reasoned with market context" from
"reasoned without it" rather than have both look identical under the same prompt_version.

Revision ID: 0096_aidy_reasoning_market_ctx
Revises: 0095_graduate_goldhunter
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0096_aidy_reasoning_market_ctx"
down_revision: str | None = "0095_graduate_goldhunter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE aidy_reasoning_annotations "
        "ADD COLUMN market_context_available boolean NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE aidy_reasoning_annotations DROP COLUMN IF EXISTS market_context_available"
    )
