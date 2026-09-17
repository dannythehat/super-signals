"""Let a newly-connected provider paper-trade only its own historically-best conditions.

The owner's own words: "we only trade their most profitable signals... we only switch
them on if we understand and can trade exactly like them." Until now there was no way to
do that -- a source is either fully shadow (nothing dispatches) or fully testing/live
(everything the AI decision engine approves dispatches). Day 17's confidence-sizing is a
deliberate, permanent no-op (`PAPER_VARIABLE_SIZING_ALLOWED = False`) precisely so
uncalibrated confidence could never silently steer real execution -- this table is
additive to that principle, not a workaround for it: it never sizes or grades a trade,
it only says whether a signal from a provider still on probation matches that same
provider's own already-computed best side (`provider_trade_fingerprints.best_side`),
using data that already existed before this signal arrived. A provider not listed here
is completely unaffected -- existing testing/live providers keep dispatching exactly as
they always have.

Revision ID: 0092_provider_probation
Revises: 0091_provider_trade_fingerprints
Create Date: 2026-09-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0092_provider_probation"
down_revision: str | None = "0091_provider_trade_fingerprints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE provider_execution_probation (
            source_id uuid PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
            enabled_at timestamptz NOT NULL DEFAULT now(),
            enabled_note text,
            -- Set true once real forward results justify trusting this provider without
            -- the side filter -- a manual graduation, never automatic.
            graduated boolean NOT NULL DEFAULT false,
            graduated_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS provider_execution_probation")
