"""Give AIDY's reasoning call an on-demand candle tool, and track real request volume.

Owner asked directly for AIDY to be able to "pull any data he requires at any time" rather
than only ever seeing a fixed prompt payload. The reasoning engine now may offer a bounded
get_recent_candles tool (real M1 bars from AIDY's existing market feed, aggregated to
whatever timeframe -- 1/5/15/30/45/60 minutes -- and lookback the model asks for, always
ending at, never after, the signal's own posted time).

Each round of tool use is a real additional OpenAI request, so what used to be "1 signal
reasoned = 1 OpenAI call" for budget-tracking purposes is no longer true once a signal
triggers a tool round-trip. request_count records how many HTTP requests one annotation's
reasoning pass actually made (1 when no tool was used, up to 3 when the model used the
tool's full allowance); tool_calls_made records how many of those were genuine tool
invocations. Both feed honest budget accounting in aidy_reasoning_runner.py instead of the
previous flat "+1 openai_calls per annotation" approximation.

Revision ID: 0097_aidy_reasoning_tool_calls
Revises: 0096_aidy_reasoning_market_ctx
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0097_aidy_reasoning_tool_calls"
down_revision: str | None = "0096_aidy_reasoning_market_ctx"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE aidy_reasoning_annotations "
        "ADD COLUMN request_count integer NOT NULL DEFAULT 1 CHECK (request_count >= 1), "
        "ADD COLUMN tool_calls_made integer NOT NULL DEFAULT 0 CHECK (tool_calls_made >= 0)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE aidy_reasoning_annotations "
        "DROP COLUMN IF EXISTS request_count, "
        "DROP COLUMN IF EXISTS tool_calls_made"
    )
