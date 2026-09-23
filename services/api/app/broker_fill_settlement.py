"""Settlement of broker fills the application failed to map to a live position.

``pending_reconciliation_canonical._persist_filled_not_visible`` deliberately refuses to
guess whether a confirmed broker fill is still open or already closed. It records the
broker position id, marks the tranche ``error`` with
``close_reason='broker_filled_position_not_visible'`` and audits
``mt5.pending_broker_fill_requires_settlement``.

That settlement was never implemented. A filled position could therefore stay live at the
broker while the application held no open row for it: untracked by management, invisible
to the owner close control (which selects ``status='open'``), absent from every
open-position view, and permanently unclosable from the product. Three such positions
accumulated on the Owner demo account over four weeks.

This module performs the missing settlement from immutable broker deals that are already
ingested. It never contacts the broker, never opens or closes anything at the broker, and
never invents state. A tranche becomes ``open`` only when the broker's own entry deal
exists with no covering exit volume, and ``closed`` only when exit volume covers the
entry. Without deal evidence the row is left exactly as it is.

It also exposes the broker-deal invariant that catches an orphan produced by any future
code path, not only this one: no broker position may hold unclosed entry volume while the
application has no ``open`` or ``pending`` row mapped to it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent

logger = logging.getLogger(__name__)

ENTRY_DEAL_TYPE = "DEAL_ENTRY_IN"
EXIT_DEAL_TYPES = ("DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY")

#: ``close_reason`` written by the pending reconciler when a confirmed fill could not be
#: mapped to a visible broker position. Settlement is scoped to rows that still carry a
#: broker position id, whatever reason stranded them.
FILLED_NOT_VISIBLE_REASON = "broker_filled_position_not_visible"
SETTLED_CLOSED_REASON = "broker_settled_closed"

ADOPT_OPEN = "adopt_open"
SETTLE_CLOSED = "settle_closed"
AWAIT_EVIDENCE = "await_evidence"

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class BrokerPositionEvidence:
    """What the immutable broker deal history says about one broker position."""

    broker_position_id: str
    entry_volume: Decimal = _ZERO
    exit_volume: Decimal = _ZERO
    first_entry_at: datetime | None = None
    last_exit_at: datetime | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    realised_cash: Decimal = _ZERO


@dataclass(frozen=True, slots=True)
class SettlementDecision:
    action: str
    reason: str


@dataclass(frozen=True, slots=True)
class SettlementResult:
    considered: int = 0
    adopted_open: int = 0
    settled_closed: int = 0
    awaiting_evidence: int = 0

    @property
    def changed(self) -> int:
        return self.adopted_open + self.settled_closed


@dataclass(frozen=True, slots=True)
class OrphanedBrokerPosition:
    """A broker position holding unclosed volume that no live local row maps to."""

    user_id: UUID
    mt5_account_id: UUID
    broker_position_id: str
    open_volume: Decimal
    first_entry_at: datetime
    local_status: str | None

    def age_seconds(self, *, now: datetime | None = None) -> float:
        point = now or datetime.now(UTC)
        entry = self.first_entry_at
        if entry.tzinfo is None:
            entry = entry.replace(tzinfo=UTC)
        return (point - entry.astimezone(UTC)).total_seconds()


def decide_settlement(evidence: BrokerPositionEvidence) -> SettlementDecision:
    """Resolve one stranded tranche against broker deal evidence.

    Volume, not deal count, decides the outcome: a partially closed position still holds
    live exposure and must return to ``open`` so management and the owner close control
    can reach the remainder.
    """
    if evidence.entry_volume <= _ZERO:
        # The broker says the order filled but no entry deal has been ingested yet.
        # Inventing an open position here would be the same guess the reconciler
        # correctly refused to make.
        return SettlementDecision(AWAIT_EVIDENCE, "no_entry_deal_ingested")
    if evidence.exit_volume <= _ZERO:
        return SettlementDecision(ADOPT_OPEN, "entry_deal_without_exit")
    if evidence.exit_volume < evidence.entry_volume:
        return SettlementDecision(ADOPT_OPEN, "exit_volume_below_entry_volume")
    return SettlementDecision(SETTLE_CLOSED, "exit_volume_covers_entry_volume")


class BrokerFillSettlementService:
    """Reconcile stranded fills against ingested broker deals. Never trades."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def settle_once(self, *, user_id: UUID | None = None) -> SettlementResult:
        rows = self._stranded_rows(user_id=user_id)
        if not rows:
            return SettlementResult()

        adopted = 0
        settled = 0
        awaiting = 0
        for row in rows:
            evidence = self._evidence(
                mt5_account_id=row["mt5_account_id"],
                broker_position_id=str(row["broker_position_id"]),
            )
            decision = decide_settlement(evidence)
            if decision.action == ADOPT_OPEN:
                self._apply_open(row, evidence, decision)
                adopted += 1
            elif decision.action == SETTLE_CLOSED:
                self._apply_closed(row, evidence, decision)
                settled += 1
            else:
                awaiting += 1

        if adopted or settled:
            logger.warning(
                "Broker fill settlement adopted=%d closed=%d awaiting=%d",
                adopted,
                settled,
                awaiting,
            )
        return SettlementResult(
            considered=len(rows),
            adopted_open=adopted,
            settled_closed=settled,
            awaiting_evidence=awaiting,
        )

    def _stranded_rows(self, *, user_id: UUID | None) -> list[dict[str, object]]:
        clause = "AND p.user_id=:user_id" if user_id is not None else ""
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    f"""
                    SELECT p.id, p.user_id, p.signal_id, p.broker_position_id,
                           p.status, p.close_reason, bd.mt5_account_id
                    FROM positions AS p
                    JOIN LATERAL (
                        SELECT d.mt5_account_id
                        FROM broker_deals AS d
                        WHERE d.broker_position_id=p.broker_position_id
                          AND d.user_id=p.user_id
                        ORDER BY d.occurred_at
                        LIMIT 1
                    ) AS bd ON TRUE
                    WHERE p.status='error'
                      AND p.broker_position_id IS NOT NULL
                      AND p.closed_at IS NULL
                      {clause}
                    ORDER BY p.created_at
                    """
                ),
                {"user_id": user_id} if user_id is not None else {},
            ).mappings().all()
        return [dict(row) for row in rows]

    def _evidence(
        self,
        *,
        mt5_account_id: object,
        broker_position_id: str,
    ) -> BrokerPositionEvidence:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        COALESCE(SUM(volume) FILTER (WHERE entry_type=:entry_type),0)
                            AS entry_volume,
                        COALESCE(SUM(volume) FILTER (WHERE entry_type=ANY(:exit_types)),0)
                            AS exit_volume,
                        MIN(occurred_at) FILTER (WHERE entry_type=:entry_type)
                            AS first_entry_at,
                        MAX(occurred_at) FILTER (WHERE entry_type=ANY(:exit_types))
                            AS last_exit_at,
                        SUM(price*volume) FILTER (WHERE entry_type=:entry_type)
                            AS entry_notional,
                        SUM(price*volume) FILTER (WHERE entry_type=ANY(:exit_types))
                            AS exit_notional,
                        COALESCE(SUM(
                            COALESCE(profit,0)+COALESCE(commission,0)+COALESCE(swap,0)
                        ) FILTER (WHERE entry_type=ANY(:exit_types)),0) AS realised_cash
                    FROM broker_deals
                    WHERE mt5_account_id=:mt5_account_id
                      AND broker_position_id=:broker_position_id
                    """
                ),
                {
                    "mt5_account_id": mt5_account_id,
                    "broker_position_id": broker_position_id,
                    "entry_type": ENTRY_DEAL_TYPE,
                    "exit_types": list(EXIT_DEAL_TYPES),
                },
            ).mappings().one()
        return _evidence_from_row(broker_position_id, dict(row))

    def _apply_open(
        self,
        row: dict[str, object],
        evidence: BrokerPositionEvidence,
        decision: SettlementDecision,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            result = session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='open',
                        opened_at=COALESCE(opened_at,:opened_at),
                        entry_price=COALESCE(:entry_price,entry_price),
                        close_reason=NULL,
                        updated_at=:now
                    WHERE id=:id AND status='error'
                    """
                ),
                {
                    "id": row["id"],
                    "opened_at": evidence.first_entry_at,
                    "entry_price": evidence.entry_price,
                    "now": now,
                },
            )
            # The UPDATE is guarded on status='error', so a row already settled by a
            # concurrent pass matches nothing. Auditing regardless would record a
            # settlement that never happened.
            if not _row_changed(result):
                return
            session.add(
                AuditEvent(
                    actor_user_id=row["user_id"],
                    event_type="mt5.broker_fill_settled_open",
                    entity_type="position",
                    entity_id=row["id"],
                    payload={
                        "broker_authoritative": True,
                        "broker_position_id": evidence.broker_position_id,
                        "previous_close_reason": row.get("close_reason"),
                        "settlement_reason": decision.reason,
                        "entry_volume": str(evidence.entry_volume),
                        "exit_volume": str(evidence.exit_volume),
                        "broker_contacted": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _apply_closed(
        self,
        row: dict[str, object],
        evidence: BrokerPositionEvidence,
        decision: SettlementDecision,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            result = session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='closed',
                        opened_at=COALESCE(opened_at,:opened_at),
                        entry_price=COALESCE(:entry_price,entry_price),
                        exit_price=COALESCE(:exit_price,exit_price),
                        pnl_amount=COALESCE(pnl_amount,:pnl_amount),
                        closed_at=COALESCE(closed_at,:closed_at),
                        close_reason=:reason,
                        updated_at=:now
                    WHERE id=:id AND status='error'
                    """
                ),
                {
                    "id": row["id"],
                    "opened_at": evidence.first_entry_at,
                    "entry_price": evidence.entry_price,
                    "exit_price": evidence.exit_price,
                    "pnl_amount": evidence.realised_cash,
                    "closed_at": evidence.last_exit_at,
                    "reason": SETTLED_CLOSED_REASON,
                    "now": now,
                },
            )
            if not _row_changed(result):
                return
            session.add(
                AuditEvent(
                    actor_user_id=row["user_id"],
                    event_type="mt5.broker_fill_settled_closed",
                    entity_type="position",
                    entity_id=row["id"],
                    payload={
                        "broker_authoritative": True,
                        "broker_position_id": evidence.broker_position_id,
                        "previous_close_reason": row.get("close_reason"),
                        "settlement_reason": decision.reason,
                        "entry_volume": str(evidence.entry_volume),
                        "exit_volume": str(evidence.exit_volume),
                        "realised_cash": str(evidence.realised_cash),
                        "broker_contacted": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def orphaned_positions(
        self,
        *,
        user_id: UUID | None = None,
    ) -> tuple[OrphanedBrokerPosition, ...]:
        """Broker positions holding unclosed volume with no live local row.

        This is the invariant that does not depend on knowing how an orphan was created.
        Any future execution path that strands a filled position is caught here, because
        the evidence is the broker's own deal history rather than application state.
        """
        clause = "AND d.user_id=:user_id" if user_id is not None else ""
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    f"""
                    SELECT d.user_id, d.mt5_account_id, d.broker_position_id,
                           COALESCE(SUM(d.volume) FILTER (WHERE d.entry_type=:entry_type),0)
                           - COALESCE(SUM(d.volume) FILTER (WHERE d.entry_type=ANY(:exit_types)),0)
                               AS open_volume,
                           MIN(d.occurred_at) FILTER (WHERE d.entry_type=:entry_type)
                               AS first_entry_at,
                           MIN(p.status) AS local_status
                    FROM broker_deals AS d
                    LEFT JOIN positions AS p
                           ON p.broker_position_id=d.broker_position_id
                          AND p.user_id=d.user_id
                          AND p.status IN ('open','pending')
                    WHERE d.broker_position_id IS NOT NULL
                      {clause}
                    GROUP BY d.user_id, d.mt5_account_id, d.broker_position_id
                    HAVING COALESCE(SUM(d.volume) FILTER (WHERE d.entry_type=:entry_type),0)
                         > COALESCE(SUM(d.volume) FILTER (WHERE d.entry_type=ANY(:exit_types)),0)
                       AND MIN(p.status) IS NULL
                    ORDER BY 5
                    """
                ),
                {
                    "entry_type": ENTRY_DEAL_TYPE,
                    "exit_types": list(EXIT_DEAL_TYPES),
                    **({"user_id": user_id} if user_id is not None else {}),
                },
            ).mappings().all()
        return tuple(
            OrphanedBrokerPosition(
                user_id=row["user_id"],
                mt5_account_id=row["mt5_account_id"],
                broker_position_id=str(row["broker_position_id"]),
                open_volume=Decimal(str(row["open_volume"] or 0)),
                first_entry_at=row["first_entry_at"],
                local_status=row["local_status"],
            )
            for row in rows
            if row["first_entry_at"] is not None
        )


def _row_changed(result: object) -> bool:
    """Did the guarded UPDATE actually modify a row?

    A driver that does not report rowcount returns -1; treat that as changed so an
    unknown outcome is still audited rather than silently dropped.
    """
    rowcount = getattr(result, "rowcount", -1)
    if not isinstance(rowcount, int):
        return True
    return rowcount != 0


def _decimal(value: object | None) -> Decimal:
    if value in (None, ""):
        return _ZERO
    return Decimal(str(value))


def _evidence_from_row(
    broker_position_id: str,
    row: dict[str, object],
) -> BrokerPositionEvidence:
    entry_volume = _decimal(row.get("entry_volume"))
    exit_volume = _decimal(row.get("exit_volume"))
    entry_notional = _decimal(row.get("entry_notional"))
    exit_notional = _decimal(row.get("exit_notional"))
    return BrokerPositionEvidence(
        broker_position_id=broker_position_id,
        entry_volume=entry_volume,
        exit_volume=exit_volume,
        first_entry_at=row.get("first_entry_at"),  # type: ignore[arg-type]
        last_exit_at=row.get("last_exit_at"),  # type: ignore[arg-type]
        entry_price=(entry_notional / entry_volume) if entry_volume > _ZERO else None,
        exit_price=(exit_notional / exit_volume) if exit_volume > _ZERO else None,
        realised_cash=_decimal(row.get("realised_cash")),
    )


__all__ = [
    "ADOPT_OPEN",
    "AWAIT_EVIDENCE",
    "BrokerFillSettlementService",
    "BrokerPositionEvidence",
    "FILLED_NOT_VISIBLE_REASON",
    "OrphanedBrokerPosition",
    "SETTLED_CLOSED_REASON",
    "SETTLE_CLOSED",
    "SettlementDecision",
    "SettlementResult",
    "decide_settlement",
]
