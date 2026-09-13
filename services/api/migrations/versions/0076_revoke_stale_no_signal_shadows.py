"""Revoke stale shadow providers that have produced no actionable signal in 30 days.

These sources had zero accepted Provider Lab signals and no actionable BUY/SELL +
TP/SL signal-like post in the prior 30-day production audit. Historical messages
remain for audit, but the sources must no longer consume Provider Lab resources.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0076_revoke_stale_no_signal"
down_revision: str = "0075_exclude_internal_ss"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REASON = "stale_no_actionable_signal_30d"
CHAT_IDS = (
    -1001906273205,  # GOLD FOREX SIGNALS (free)
    -1003659707176,  # Gold Automation | XAUUSD
    -1003897642773,  # Alin Trades
    -1002726923216,  # Accurate Signals VIP
    -1002552646530,  # Prestige Gold Traders | Free Signals
    -1001284741400,  # Xauusd Gold Trading Free Signals
    -1002871898226,  # GOLD MASTERY – Struktur, Signal & Succes
    -1002173061244,  # Forex MasterMind
    -1001360867436,  # Gold Signals Daily
    -1002233338323,  # GTC Scalping Ideas
    -1002091667950,  # XAUUSD (GOLD) PIPS SCALPING
)


def upgrade() -> None:
    ids = ",".join(str(v) for v in CHAT_IDS)
    op.execute(
        f"""
        UPDATE sources
        SET status = 'revoked',
            permission_notes = CASE
                WHEN COALESCE(permission_notes, '') LIKE '%{REASON}%'
                    THEN permission_notes
                ELSE CONCAT_WS(E'\\n', NULLIF(permission_notes, ''),
                    '{REASON}: removed from active Provider Lab after production audit')
            END,
            updated_at = now()
        WHERE chat_id IN ({ids})
          AND status = 'shadow';
        """
    )

    # Preserve any historical research rows but prevent them from ever entering
    # provider scoring if older/backfilled shadow rows exist for these sources.
    op.execute(
        f"""
        UPDATE shadow_trades
        SET score_eligible = false,
            score_exclusion_reason = '{REASON}',
            aidy_score_blocked = true,
            updated_at = now()
        WHERE source_id IN (SELECT id FROM sources WHERE chat_id IN ({ids}));
        """
    )


def downgrade() -> None:
    # Deliberately do not auto-reactivate retired external groups.
    pass
