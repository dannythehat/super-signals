"""Graduate GOLDHUNTER off probation at the owner's explicit, informed direction.

The owner reviewed GOLDHUNTER's verified forward track record (43 resolved paper
trades, 79.1% win rate, both BUY and SELL individually well past the cohort sample
floor: SELL 80.0% over 20 trades, BUY 78.3% over 23 trades) and asked directly to
graduate her fully -- both sides trading, and eligible for real member live-money
accounts, not just demo/paper -- understanding that only minutes of real forward
history exist under the corrected side/session gating (0094_fp_side_session_split)
before this takes effect. This is the owner's call to make, matching the exact
mechanism ``provider_execution_probation.enabled_note`` (migration 0093) always said
graduation would be: "until the owner reviews real forward results and graduates it."

Graduating clears both restrictions ``check_probation_eligibility`` and
``is_active_probation`` apply for a non-graduated source: the single-best-side
match requirement, and the demo/paper-only cap on member distribution. Real member
live-money execution for her signals still additionally requires the global
``SUPER_SIGNALS_LIVE_EXECUTION_ENABLED`` switch and each member's own opt-in --
this migration only removes GOLDHUNTER's own provider-level restriction.

Revision ID: 0095_graduate_goldhunter
Revises: 0094_fp_side_session_split
Create Date: 2026-09-18
"""

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text
from sqlalchemy.orm import Session

revision: str = "0095_graduate_goldhunter"
down_revision: str | None = "0094_fp_side_session_split"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHAT_ID = -1002528249483
_CHAT_TITLE = "GOLDHUNTER | PAUL \U0001f981 FX & CRYPTO \U0001f30d"


def upgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    source_id = session.execute(
        text("SELECT id FROM sources WHERE chat_id = :chat_id"),
        {"chat_id": _CHAT_ID},
    ).scalar_one_or_none()
    if source_id is None:
        return

    updated = session.execute(
        text(
            "UPDATE provider_execution_probation "
            "SET graduated = true, graduated_at = now() "
            "WHERE source_id = :source_id AND graduated = false "
            "RETURNING source_id"
        ),
        {"source_id": source_id},
    ).scalar_one_or_none()
    if updated is None:
        return

    payload = json.dumps(
        {
            "chat_title": _CHAT_TITLE,
            "reason": "owner_reviewed_forward_results_and_graduated",
            "trades_resolved": 43,
            "win_rate_pct": 79.1,
            "best_side_win_rate_pct": 80.0,
            "worst_side_win_rate_pct": 78.3,
        }
    )
    session.execute(
        text(
            "INSERT INTO audit_events (actor_user_id, event_type, entity_type, entity_id, payload) "
            "VALUES (NULL, 'provider_probation.graduated', 'source', :source_id, "
            "CAST(:payload AS jsonb))"
        ),
        {"source_id": source_id, "payload": payload},
    )
    session.commit()


def downgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)
    source_id = session.execute(
        text("SELECT id FROM sources WHERE chat_id = :chat_id"),
        {"chat_id": _CHAT_ID},
    ).scalar_one_or_none()
    if source_id is None:
        return
    session.execute(
        text(
            "UPDATE provider_execution_probation "
            "SET graduated = false, graduated_at = NULL "
            "WHERE source_id = :source_id"
        ),
        {"source_id": source_id},
    )
    session.commit()
