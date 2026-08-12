"""Temporary opt-in Day 33 live acceptance proof.

This probe has no trading gateway. It reads broker history through MetaApiReadGateway,
stores immutable deals, rebuilds derived outcomes/summaries twice, and records only
safe counts/digests in the audit log.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_read_gateway import MetaApiReadGateway
from app.models import AuditEvent
from app.mt5_crypto import MetaApiTokenCipher
from app.performance_ledger_day33 import Day33LedgerError
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2

logger = logging.getLogger(__name__)


async def run_day33_live_acceptance(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
    owner_user_id: UUID,
) -> None:
    service = Day33PerformanceLedgerServiceV2(
        session_factory=session_factory,
        cipher=cipher,
        gateway=MetaApiReadGateway(),
    )
    try:
        first = await service.sync_user(owner_user_id)
        first_digest = _summary_digest(session_factory, owner_user_id)
        second = await service.sync_user(owner_user_id)
        second_digest = _summary_digest(session_factory, owner_user_id)
    except Day33LedgerError as exc:
        logger.error(
            "Day 33 live acceptance failed code=%s retryable=%s trade_action_created=false",
            exc.code,
            exc.retryable,
        )
        _record(
            session_factory,
            owner_user_id,
            {
                "passed": False,
                "error_code": exc.code,
                "retryable": exc.retryable,
                "trade_action_created": False,
            },
        )
        return

    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT
                    COUNT(*)::int AS deal_count,
                    COUNT(DISTINCT (mt5_account_id,broker_deal_id))::int AS distinct_deal_count,
                    COUNT(*) FILTER (WHERE position_id IS NOT NULL)::int AS mapped_deal_count
                FROM broker_deals
                WHERE user_id=:user_id
                """
            ),
            {"user_id": owner_user_id},
        ).mappings().one()
        outcome = session.execute(
            text(
                """
                SELECT
                    COUNT(*)::int AS outcome_count,
                    COUNT(*) FILTER (WHERE status IN ('won','lost','breakeven'))::int AS known_count,
                    COUNT(*) FILTER (WHERE status='closed_unknown')::int AS unknown_count,
                    COUNT(*) FILTER (WHERE status='open')::int AS open_count,
                    COUNT(*) FILTER (WHERE status='pending')::int AS pending_count
                FROM performance_trade_outcomes
                WHERE user_id=:user_id
                """
            ),
            {"user_id": owner_user_id},
        ).mappings().one()
        summary_count = int(
            session.execute(
                text("SELECT COUNT(*) FROM performance_summaries WHERE user_id=:user_id"),
                {"user_id": owner_user_id},
            ).scalar_one()
        )
        position_count = int(
            session.execute(
                text("SELECT COUNT(*) FROM positions WHERE user_id=:user_id AND broker_position_id IS NOT NULL"),
                {"user_id": owner_user_id},
            ).scalar_one()
        )

    deal_count = int(row["deal_count"])
    distinct_deal_count = int(row["distinct_deal_count"])
    mapped_deal_count = int(row["mapped_deal_count"])
    outcome_count = int(outcome["outcome_count"])
    known_count = int(outcome["known_count"])
    unknown_count = int(outcome["unknown_count"])
    open_count = int(outcome["open_count"])
    pending_count = int(outcome["pending_count"])
    stable = first_digest == second_digest
    dedup = deal_count == distinct_deal_count
    complete_coverage = outcome_count == position_count
    second_added_zero = second.broker_deals_added == 0
    passed = (
        deal_count > 0
        and mapped_deal_count == deal_count
        and known_count > 0
        and summary_count > 0
        and stable
        and dedup
        and complete_coverage
        and second_added_zero
        and first.broker_trade_action_created is False
        and second.broker_trade_action_created is False
    )
    payload = {
        "passed": passed,
        "first_deals_added": first.broker_deals_added,
        "second_deals_added": second.broker_deals_added,
        "first_positions_checked": first.mapped_positions_checked,
        "second_positions_checked": second.mapped_positions_checked,
        "deal_count": deal_count,
        "distinct_deal_count": distinct_deal_count,
        "mapped_deal_count": mapped_deal_count,
        "position_count": position_count,
        "outcome_count": outcome_count,
        "known_outcome_count": known_count,
        "closed_unknown_count": unknown_count,
        "open_count": open_count,
        "pending_count": pending_count,
        "summary_count": summary_count,
        "summary_regeneration_stable": stable,
        "summary_digest": second_digest,
        "broker_deal_dedup_passed": dedup,
        "outcome_coverage_passed": complete_coverage,
        "second_sync_added_zero_deals": second_added_zero,
        "broker_trade_action_created": False,
    }
    _record(session_factory, owner_user_id, payload)
    logger.info(
        "Day 33 live acceptance completed passed=%s deals=%d known=%d unknown=%d summaries=%d stable=%s dedup=%s trade_action_created=false",
        passed,
        deal_count,
        known_count,
        unknown_count,
        summary_count,
        stable,
        dedup,
    )


def _summary_digest(session_factory: sessionmaker[Session], user_id: UUID) -> str:
    with session_factory() as session:
        values = session.execute(
            text(
                """
                SELECT period_type,period_start,dimension_type,dimension_key,total_trades,
                       wins,losses,breakeven,open_trades,cash_pnl,return_percent,net_pips,
                       model_500_pnl,model_500_return_percent,source_digest
                FROM performance_summaries
                WHERE user_id=:user_id
                ORDER BY period_type,period_start,dimension_type,dimension_key
                """
            ),
            {"user_id": user_id},
        ).all()
    import hashlib
    import json

    normalized = [[str(value) for value in row] for row in values]
    return hashlib.sha256(
        json.dumps(normalized, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _record(
    session_factory: sessionmaker[Session],
    owner_user_id: UUID,
    payload: dict[str, object],
) -> None:
    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_user_id,
                event_type="day33.live_acceptance_completed",
                entity_type="performance_ledger",
                payload=payload,
            )
        )
        session.commit()
