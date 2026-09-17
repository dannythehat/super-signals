"""Switch on the shadow providers with real, resolved, positive scored history for paper trading.

The owner's own words: "we can paper trade them... until Aidy is super confident of
calling the market" and "we only switch them on if we understand and can trade exactly
like them." The TIG investigation (see 0080_shadow_tig_asia.py and the
``incident_20260904_asia_owner_cleanup``/``incident_20260909_tig_visibility_quarantine``
scripts) proved a scored/idealized number can disagree sharply with real executed P&L, so
none of these six are switched on as trusted -- each is moved to ``testing`` (paper, the
single shared demo account) AND placed on ``provider_execution_probation`` in the same
migration, which restricts real dispatch to that provider's own already-computed
best-evidenced side (``provider_trade_fingerprints.best_side``) and, for the two with too
little side/session evidence yet, holds every signal back until there is enough. Nothing
here grants LIVE execution: the canonical dispatcher never routes a probationary source's
new_trade to a member's real account (management_reliability_runtime / probation checks
in execution_dispatch_canonical.py), so this can only ever move real money once the owner
reviews real forward paper results and explicitly graduates a provider.

TIG's Asia Trades is included this time, but only because the actual root cause of its
real losses -- a management/close instruction that could not be matched to a broker
position getting silently dropped instead of retried or failed safe -- is now fixed
(``management_reliability_runtime.py``, ``Day27Mt5ManagementService.force_close_all_positions``).
It goes back in on the same probation footing as every other provider here: paper only,
restricted to its own best-evidenced side (SELL, per its current fingerprint), never
assumed trustworthy just because the reliability bug is gone.

Selection: every currently-shadow source with >= 8 resolved scored trades and positive
net_pnl_usd in provider_trade_scoreboard as of 2026-09-17 (queried directly against
production, not reproduced as SQL here since the scoreboard is itself a derived view).

Revision ID: 0093_switch_on_profitable
Revises: 0092_provider_probation
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0093_switch_on_profitable"
down_revision: str | None = "0092_provider_probation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDERS = (
    (-1002528249483, "GOLDHUNTER | PAUL 🦁 FX & CRYPTO 🌍"),
    (-1001957768371, "FREE TRADİNG SİGNALS"),
    (-1003368777331, "XAUUSD SIGNALS"),
    (-1004469449988, "Scalping 📈"),
    (-1002148698027, "XAUUSD JULIA"),
    (-1003680830069, "TIG’s Asia Trades"),
)

_ENABLED_NOTE = (
    "Switched on for paper trading after the TIG real-vs-scored investigation. "
    "Restricted to this provider's own best-evidenced side via provider_trade_fingerprints "
    "until the owner reviews real forward results and graduates it."
)


def upgrade() -> None:
    bind = op.get_bind()
    for chat_id, title in _PROVIDERS:
        row = bind.execute(
            sa.text(
                """
                WITH target AS (
                    SELECT id, status AS previous_status
                    FROM sources
                    WHERE chat_id=:chat_id
                      AND status='shadow'
                    ORDER BY created_at ASC
                    LIMIT 1
                ), changed AS (
                    UPDATE sources AS s
                    SET status='testing', updated_at=now()
                    FROM target AS t
                    WHERE s.id=t.id
                    RETURNING s.id, t.previous_status
                )
                SELECT id, previous_status FROM changed
                """
            ),
            {"chat_id": chat_id},
        ).mappings().one_or_none()
        if row is None:
            # Already not-shadow (re-run, or the owner changed it manually since) --
            # never clobber a status this migration did not itself just set.
            continue

        source_id = row["id"]
        bind.execute(
            sa.text(
                """
                INSERT INTO provider_execution_probation (source_id, enabled_note)
                VALUES (:source_id, :note)
                ON CONFLICT (source_id) DO NOTHING
                """
            ),
            {"source_id": source_id, "note": _ENABLED_NOTE},
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO audit_events(event_type, entity_type, entity_id, payload)
                VALUES (
                    'telegram.source_status_changed',
                    'source',
                    :source_id,
                    jsonb_build_object(
                        'source_title', :title,
                        'previous_status', :previous_status,
                        'status', 'testing',
                        'actor_role', 'system_migration',
                        'actor_display_name', 'Super Signals migration',
                        'changed_at', now(),
                        'monitoring_started', true,
                        'live_trading_enabled', false,
                        'probation_enabled', true,
                        'reason', 'owner_enabled_paper_trading_after_tig_reliability_review'
                    )
                )
                """
            ),
            {
                "source_id": source_id,
                "title": title,
                "previous_status": row["previous_status"],
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    for chat_id, _title in _PROVIDERS:
        bind.execute(
            sa.text(
                """
                UPDATE sources
                SET status='shadow', updated_at=now()
                WHERE chat_id=:chat_id AND status='testing'
                """
            ),
            {"chat_id": chat_id},
        )
        bind.execute(
            sa.text(
                """
                DELETE FROM provider_execution_probation
                WHERE source_id IN (SELECT id FROM sources WHERE chat_id=:chat_id)
                  AND graduated=false
                """
            ),
            {"chat_id": chat_id},
        )
