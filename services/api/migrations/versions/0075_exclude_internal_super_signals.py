"""Exclude the internal Super Signals Telegram output from Provider Lab.

Smart Signals / Super Signals is our own generated output, not an external signal
provider. It must never be shadow-watched, benchmarked, ranked, or used as provider
performance evidence.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0075_exclude_internal_ss"
down_revision: str = "0074_fix_fxvision_scale"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INTERNAL_CHAT_ID = -1004481439217
EXCLUSION_REASON = "internal_super_signals_source"


def upgrade() -> None:
    # Revoke the internal Telegram output so every active-source pipeline that
    # requires testing/shadow/live stops consuming it immediately.
    op.execute(
        f"""
        UPDATE sources
        SET status = 'revoked',
            permission_notes = CASE
                WHEN COALESCE(permission_notes, '') LIKE '%internal_super_signals_source%'
                    THEN permission_notes
                ELSE CONCAT_WS(E'\\n', NULLIF(permission_notes, ''),
                    'internal_super_signals_source: own Super Signals output; never provider evidence')
            END,
            updated_at = now()
        WHERE chat_id = {INTERNAL_CHAT_ID};
        """
    )

    # Historical rows stay in the audit trail, but are permanently ineligible
    # for provider scoring/ranking so they cannot contaminate statistics even if
    # queried without a source-status filter.
    op.execute(
        f"""
        UPDATE shadow_trades
        SET score_eligible = false,
            score_exclusion_reason = '{EXCLUSION_REASON}',
            aidy_score_blocked = true,
            updated_at = now()
        WHERE source_id IN (
            SELECT id FROM sources WHERE chat_id = {INTERNAL_CHAT_ID}
        );
        """
    )


def downgrade() -> None:
    # Safety migration: intentionally do not reactivate our own output or make
    # its historical shadow rows scoreable again.
    pass
