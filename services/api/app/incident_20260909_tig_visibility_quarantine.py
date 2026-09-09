"""Quarantine the 9 September TIG trade that was not reliably visible to the owner.

This is a one-shot forensic repair. It never deletes Telegram messages, positions,
broker deals, performance outcomes, or publication evidence. Instead, the exact executed
signal and its derived broker/performance rows are relinked to a dedicated revoked
incident source. Production reporting already excludes revoked sources, so the incident
is removed from the owner's app, balance, charts and performance figures while the raw
evidence remains available for investigation.

The original TIG source remains untouched and active for future messages.
"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from app.db import get_session_factory

INCIDENT_KEY = "incident-2026-09-09-tig-1968-invisible-live-trade"
TRADE_REF = "SS-CB190F3CC4"
PROVIDER_MESSAGE_ID = 1968
QUARANTINE_CHAT_ID = -9223372036854775000
EXPECTED_CASH_PNL = Decimal("-85.68")
EXPECTED_OUTCOMES = 4
EXPECTED_BROKER_DEALS = 8


def run() -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        owner_user_id = session.execute(
            text(
                """
                SELECT u.id
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id=u.id
                JOIN roles AS r ON r.id=ur.role_id
                WHERE r.name='owner'
                ORDER BY u.created_at
                LIMIT 1
                """
            )
        ).scalar_one_or_none()
        if owner_user_id is None:
            raise RuntimeError("tig_visibility_quarantine_owner_missing")

        already_done = session.execute(
            text(
                """
                SELECT 1
                FROM audit_events
                WHERE actor_user_id=:user_id
                  AND event_type='performance.signal_incident_quarantined'
                  AND payload->>'incident_key'=:incident_key
                LIMIT 1
                """
            ),
            {"user_id": owner_user_id, "incident_key": INCIDENT_KEY},
        ).scalar_one_or_none()
        if already_done is not None:
            print("SUPER_SIGNALS_TIG_VISIBILITY_QUARANTINE=ALREADY_DONE", flush=True)
            return

        target = session.execute(
            text(
                """
                SELECT
                    s.id AS signal_id,
                    s.source_id,
                    s.provider_message_id,
                    s.source_posted_at,
                    src.telegram_account_id,
                    src.chat_id,
                    src.chat_title,
                    src.source_alias,
                    src.created_by_user_id
                FROM signals AS s
                JOIN sources AS src ON src.id=s.source_id
                WHERE src.chat_title='TIG’s Asia Trades'
                  AND s.provider_message_id=:provider_message_id
                  AND s.source_posted_at>=TIMESTAMPTZ '2026-09-09 15:15:00+00'
                  AND s.source_posted_at<TIMESTAMPTZ '2026-09-09 15:17:00+00'
                """
            ),
            {"provider_message_id": PROVIDER_MESSAGE_ID},
        ).mappings().all()
        if len(target) != 1:
            raise RuntimeError(
                f"tig_visibility_quarantine_target_count_invalid:{len(target)}"
            )
        row = target[0]
        signal_id = row["signal_id"]
        original_source_id = row["source_id"]

        outcome_summary = session.execute(
            text(
                """
                SELECT COUNT(*)::int AS outcome_count,
                       COALESCE(SUM(cash_pnl),0) AS cash_pnl
                FROM performance_trade_outcomes
                WHERE user_id=:user_id AND signal_id=:signal_id
                """
            ),
            {"user_id": owner_user_id, "signal_id": signal_id},
        ).mappings().one()
        outcome_count = int(outcome_summary["outcome_count"] or 0)
        cash_pnl = Decimal(str(outcome_summary["cash_pnl"] or 0)).quantize(Decimal("0.01"))
        if outcome_count != EXPECTED_OUTCOMES or cash_pnl != EXPECTED_CASH_PNL:
            raise RuntimeError(
                "tig_visibility_quarantine_outcome_evidence_changed:"
                f"count={outcome_count}:cash={cash_pnl}"
            )

        broker_deal_count = int(
            session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM broker_deals
                    WHERE user_id=:user_id AND signal_id=:signal_id
                    """
                ),
                {"user_id": owner_user_id, "signal_id": signal_id},
            ).scalar_one()
            or 0
        )
        if broker_deal_count != EXPECTED_BROKER_DEALS:
            raise RuntimeError(
                "tig_visibility_quarantine_broker_evidence_changed:"
                f"deals={broker_deal_count}"
            )

        quarantine_source_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO sources (
                    id, telegram_account_id, chat_id, chat_title, source_alias, status,
                    redistribution_permission_confirmed, permission_notes,
                    created_by_user_id, created_at, updated_at
                ) VALUES (
                    :id, :telegram_account_id, :chat_id, :chat_title, :source_alias,
                    'revoked', false, :permission_notes, :created_by_user_id, now(), now()
                )
                """
            ),
            {
                "id": quarantine_source_id,
                "telegram_account_id": row["telegram_account_id"],
                "chat_id": QUARANTINE_CHAT_ID,
                "chat_title": "Incident quarantine · TIG 1968 · 9 Sep 2026",
                "source_alias": f"Incident quarantine · {TRADE_REF}",
                "permission_notes": (
                    "Forensic quarantine only. Original TIG Asia Telegram message and "
                    "broker evidence retained. Excluded from user-facing Super Signals "
                    "execution performance after live-visibility incident."
                ),
                "created_by_user_id": row["created_by_user_id"],
            },
        )

        moved_signals = session.execute(
            text(
                """
                UPDATE signals
                SET source_id=:quarantine_source_id
                WHERE id=:signal_id AND source_id=:original_source_id
                RETURNING id
                """
            ),
            {
                "quarantine_source_id": quarantine_source_id,
                "signal_id": signal_id,
                "original_source_id": original_source_id,
            },
        ).scalars().all()
        if len(moved_signals) != 1:
            raise RuntimeError("tig_visibility_quarantine_signal_move_failed")

        moved_outcomes = session.execute(
            text(
                """
                UPDATE performance_trade_outcomes
                SET source_id=:quarantine_source_id
                WHERE user_id=:user_id AND signal_id=:signal_id
                RETURNING position_id
                """
            ),
            {
                "quarantine_source_id": quarantine_source_id,
                "user_id": owner_user_id,
                "signal_id": signal_id,
            },
        ).scalars().all()
        if len(moved_outcomes) != EXPECTED_OUTCOMES:
            raise RuntimeError(
                f"tig_visibility_quarantine_outcome_move_failed:{len(moved_outcomes)}"
            )

        moved_deals = session.execute(
            text(
                """
                UPDATE broker_deals
                SET source_id=:quarantine_source_id
                WHERE user_id=:user_id AND signal_id=:signal_id
                RETURNING id
                """
            ),
            {
                "quarantine_source_id": quarantine_source_id,
                "user_id": owner_user_id,
                "signal_id": signal_id,
            },
        ).scalars().all()
        if len(moved_deals) != EXPECTED_BROKER_DEALS:
            raise RuntimeError(
                f"tig_visibility_quarantine_broker_move_failed:{len(moved_deals)}"
            )

        payload = {
            "incident_key": INCIDENT_KEY,
            "trade_ref": TRADE_REF,
            "provider": "TIG’s Asia Trades",
            "provider_message_id": PROVIDER_MESSAGE_ID,
            "signal_id": str(signal_id),
            "original_source_id": str(original_source_id),
            "original_chat_id": int(row["chat_id"]),
            "original_source_posted_at": row["source_posted_at"].isoformat(),
            "quarantine_source_id": str(quarantine_source_id),
            "cash_pnl": str(cash_pnl),
            "broker_deals_relinked": len(moved_deals),
            "outcomes_relinked": len(moved_outcomes),
            "reason": (
                "Trade executed and settled at broker while the user-facing app did not "
                "reliably expose the live position. Exclude from official Super Signals "
                "execution performance without deleting message, position, broker-deal, "
                "outcome or Telegram-publication evidence."
            ),
            "raw_evidence_deleted": False,
            "user_facing_excluded": True,
        }
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    actor_user_id,event_type,entity_type,entity_id,payload,created_at
                ) VALUES (
                    :user_id,'performance.signal_incident_quarantined','signal',
                    :signal_id,CAST(:payload AS jsonb),now()
                )
                """
            ),
            {
                "user_id": owner_user_id,
                "signal_id": signal_id,
                "payload": json.dumps(payload, sort_keys=True),
            },
        )
        session.commit()

    print(
        "SUPER_SIGNALS_TIG_VISIBILITY_QUARANTINE=PASS "
        f"trade={TRADE_REF} cash_removed={EXPECTED_CASH_PNL} "
        f"outcomes={EXPECTED_OUTCOMES} broker_deals={EXPECTED_BROKER_DEALS}",
        flush=True,
    )


if __name__ == "__main__":
    run()
