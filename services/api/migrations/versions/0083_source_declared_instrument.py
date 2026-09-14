"""Let a source declare its instrument in data instead of a hardcoded allowlist.

Which sources may have a missing instrument filled in is currently decided by a set of
five profile ids in ``v1_message_policy``, fed by a dictionary of nine chat titles in
``provider_language_profiles``. Both are hand-written and neither grew with the
business, so of the 33 groups now connected only the original handful can benefit.
GOLDHUNTER publishes complete signals such as "Buy at 4392.63 SL 4377.63 4403 4413
4423" and every one is refused for not containing the word gold.

The two lists have also drifted apart. ``ajd_xauusd`` declares ``instrument: XAUUSD`` in
its profile but is absent from the policy set, so its declaration does nothing, while
``matthew_xauusd`` sits in the policy set with no profile entry that can ever produce it.
Neither is changed here; moving the decision into data is what stops the drift
recurring.

``declared_instrument`` is owner-set per source and never inferred. Titles are not
evidence: a channel named "FOREX GOLD XAUUSD SIGNALS" posts "NEW #USOIL SELL SIGNAL", so
deriving gold from a name would have turned an oil signal into a gold trade. Nothing is
enabled by this migration. Existing gold sources are seeded with the identity they
already have, so behaviour is unchanged, and every other source stays null until the
owner says otherwise.

A declaration only ever fills a silence. A message naming a different instrument is
refused by the foreign-instrument rule regardless of what its source declares.

Revision ID: 0083_source_declared_instrument
Revises: 0082_backfill_trade_observations
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083_source_declared_instrument"
down_revision: str | None = "0082_backfill_trade_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Chat titles whose profile already supplies XAUUSD today. Seeding these changes no
# behaviour; it records in data what was previously implied by a hardcoded set.
_ALREADY_GOLD = (
    "the gold club - tgc",
    "tdc v2 💎 (new)",
    "tig’s asia trades",
    "tig's asia trades",
    "sureshot gold",
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE sources
        ADD COLUMN declared_instrument varchar(24)
        CHECK (declared_instrument IS NULL OR declared_instrument IN ('XAUUSD'))
        """
    )
    # Bound, not interpolated: one of these titles contains an apostrophe.
    op.get_bind().execute(
        sa.text(
            """
            UPDATE sources
            SET declared_instrument='XAUUSD', updated_at=now()
            WHERE lower(COALESCE(NULLIF(chat_title,''),source_alias)) = ANY(:titles)
            """
        ),
        {"titles": list(_ALREADY_GOLD)},
    )


def downgrade() -> None:
    op.execute("ALTER TABLE sources DROP COLUMN IF EXISTS declared_instrument")
