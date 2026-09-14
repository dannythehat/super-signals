"""Declare XAUUSD for shadow sources whose own posts show they trade gold.

Evidence from production on 2026-09-14, counting trades the interpreter understood and
refused only for ``missing_instrument`` since 2026-08-26, against messages naming an
instrument we do not trade:

    GOLD VIP (XAUUSD)        41 recoverable, 0 foreign
    GOLDHUNTER | PAUL        43 recoverable, 7 foreign
    Gold Trader Mo           16 recoverable, 1 foreign
    Satoshi Trader Gold       6 recoverable, 0 foreign
    XAUUSD SIGNALS            4 recoverable, 2 foreign

The evidence is each source's own message history, not its name. Names were checked and
rejected as a signal: "FOREX GOLD XAUUSD SIGNALS" posts "NEW #USOIL SELL SIGNAL", and it
is deliberately not declared here. Sources that genuinely trade several markets are also
left alone -- Learn 2 Trade shows 14 foreign posts against 2 recoverable trades, and
FXTradingVision 18 against 1.

Every source here is ``shadow``. The canonical dispatcher branches shadow sources into
_dispatch_shadow before any broker code path exists, so this changes what research can
see and cannot change what trades. The two live-executing candidates, GTMO VIP with 20
recoverable and FXTradingVision, are deliberately excluded: declaring an instrument for
a source that executes is an owner decision about real money.

The handful of foreign posts in these sources are not a reason to withhold the
declaration. A declaration only fills a silence, and a message naming Bitcoin, oil or a
currency pair is refused by the foreign-instrument rule regardless of what its source
declares.

Revision ID: 0084_declare_gold_shadow_sources
Revises: 0083_source_declared_instrument
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0084_declare_gold_shadow_sources"
down_revision: str | None = "0083_source_declared_instrument"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GOLD_SHADOW_CHAT_IDS = (
    "GOLD VIP (XAUUSD)",
    "GOLDHUNTER | PAUL 🦁 FX & CRYPTO 🌍",
    "Gold Trader Mo🤴🏽",
    "Satoshi Trader Gold 🏆",
    "XAUUSD SIGNALS",
)


def upgrade() -> None:
    # Restricted to shadow status in the statement itself: if one of these has been
    # promoted since this was written, it must not be silently declared on the way past.
    op.get_bind().execute(
        sa.text(
            """
            UPDATE sources
            SET declared_instrument='XAUUSD', updated_at=now()
            WHERE COALESCE(NULLIF(chat_title,''),source_alias) = ANY(:titles)
              AND status='shadow'
              AND declared_instrument IS NULL
            """
        ),
        {"titles": list(_GOLD_SHADOW_CHAT_IDS)},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE sources
            SET declared_instrument=NULL, updated_at=now()
            WHERE COALESCE(NULLIF(chat_title,''),source_alias) = ANY(:titles)
            """
        ),
        {"titles": list(_GOLD_SHADOW_CHAT_IDS)},
    )
