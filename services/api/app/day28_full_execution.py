"""Day 28 automatic Telegram -> AI/V1 -> Vantage demo orchestration.

Day 21 already persists each live Telegram message and stores the final AI/V1 decision.
This module reads that durable decision and dispatches only two mechanically approved
paths:

* ``new_trade / execute`` -> the existing atomic Day 26 demo executor
* ``trade_update / apply_update`` -> the existing Day 27 demo management service

Everything else is ignored.  The dispatcher never reparses provider text, changes an
entry/SL/TP, or broadens source eligibility.  Day 26/27 remain the broker safety
boundary.  Day 28 only connects the already-proven pieces.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_management_day27 import Day27ManagementError

logger = logging.getLogger(__name__)

_ALLOWED_RISK = {Decimal("0.5"), Decimal("1"), Decimal("1.5"), Decimal("2")}


@dataclass(frozen=True, slots=True)
class Day28RouteResult:
    outcome: str
    decision: str | None
    action: str | None
    signal_id: UUID | None = None
    lifecycle_event_id: UUID | None = None
    position_count: int = 0
    broker_actions_sent: int = 0
    already_applied: bool = False
    error_code: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class _StoredDecision:
    message_id: UUID
    decision: str
    action: str
    reason: str


class Day28FullExecutionRouter:
    """Dispatch one already-supervised live Telegram message to Day 26 or Day 27."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        owner_user_id: UUID,
        execution_service: Any,
        management_service: Any,
        allowed_source_ids: Iterable[UUID],
        risk_percent: Decimal | str = Decimal("1"),
        double_lot_approved: bool = True,
    ) -> None:
        risk = Decimal(str(risk_percent))
        if risk not in _ALLOWED_RISK:
            raise ValueError("day28_risk_percent_invalid")
        source_ids = frozenset(allowed_source_ids)
        if not source_ids:
            raise ValueError("day28_source_allowlist_required")

        self._session_factory = session_factory
        self._owner_user_id = owner_user_id
        self._execution = execution_service
        self._management = management_service
        self._allowed_source_ids = source_ids
        self._risk_percent = risk
        self._double_lot_approved = bool(double_lot_approved)
        self._locks: dict[str, asyncio.Lock] = {}

    async def dispatch_stored_decision(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int = 0,
    ) -> Day28RouteResult:
        """Apply the durable final decision for one live Telegram delivery.

        This method is intentionally not used by the bounded reconnect catch-up path.
        Historical messages may be supervised/audited during catch-up, but broker
        mutation belongs only to a live delivery (or its live edit) in Day 28.
        """
        if source_id not in self._allowed_source_ids:
            return Day28RouteResult(
                outcome="ignored",
                decision=None,
                action=None,
                reason="source_not_in_day28_allowlist",
            )

        stored = self._load_stored_decision(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )
        if stored is None:
            return Day28RouteResult(
                outcome="blocked",
                decision=None,
                action=None,
                error_code="day28_stored_decision_missing",
                reason="day28_stored_decision_missing",
            )

        if stored.decision == "new_trade" and stored.action == "execute":
            return await self._dispatch_new_trade(stored, revision_index)

        if stored.decision == "trade_update" and stored.action == "apply_update":
            return await self._dispatch_management(stored, revision_index)

        return Day28RouteResult(
            outcome="ignored",
            decision=stored.decision,
            action=stored.action,
            reason=stored.reason,
        )

    async def _dispatch_new_trade(
        self,
        stored: _StoredDecision,
        revision_index: int,
    ) -> Day28RouteResult:
        signal_id = self._resolve_signal_id(stored.message_id, revision_index)
        if signal_id is None:
            self._audit_failure(
                entity_id=stored.message_id,
                entity_type="message",
                error_code="day28_signal_not_resolved",
                decision=stored.decision,
                action=stored.action,
            )
            return Day28RouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                error_code="day28_signal_not_resolved",
                reason="day28_signal_not_resolved",
            )

        lock = self._locks.setdefault(f"signal:{signal_id}", asyncio.Lock())
        async with lock:
            if self._has_position_records(signal_id):
                return Day28RouteResult(
                    outcome="already_applied",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    position_count=self._position_count(signal_id),
                    already_applied=True,
                    reason="signal_execution_already_started",
                )

            try:
                result = await self._execution.execute_owner_demo_signal(
                    owner_user_id=self._owner_user_id,
                    signal_id=signal_id,
                    risk_percent=str(self._risk_percent),
                    double_lot_approved=self._double_lot_approved,
                )
            except Day26ExecutionError as exc:
                # A duplicate live delivery can race the first delivery.  Day 26's
                # own position gate is authoritative; once position records exist the
                # replay is successful idempotency, not a second broker attempt.
                if exc.code == "signal_execution_already_started" and self._has_position_records(
                    signal_id
                ):
                    return Day28RouteResult(
                        outcome="already_applied",
                        decision=stored.decision,
                        action=stored.action,
                        signal_id=signal_id,
                        position_count=self._position_count(signal_id),
                        already_applied=True,
                        reason=exc.code,
                    )
                self._audit_failure(
                    entity_id=signal_id,
                    entity_type="signal",
                    error_code=exc.code,
                    decision=stored.decision,
                    action=stored.action,
                )
                return Day28RouteResult(
                    outcome="blocked",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    error_code=exc.code,
                    reason=exc.code,
                )

            position_count = len(result.positions)
            self._audit_success(
                entity_id=signal_id,
                entity_type="signal",
                payload={
                    "route": "new_trade",
                    "source_revision_index": revision_index,
                    "risk_percent": str(self._risk_percent),
                    "double_lot_approved": self._double_lot_approved,
                    "double_lot_applied": bool(result.double_lot_applied),
                    "position_count": position_count,
                    "automatic_execution": True,
                },
            )
            return Day28RouteResult(
                outcome="executed",
                decision=stored.decision,
                action=stored.action,
                signal_id=signal_id,
                position_count=position_count,
                reason="day28_new_trade_executed",
            )

    async def _dispatch_management(
        self,
        stored: _StoredDecision,
        revision_index: int,
    ) -> Day28RouteResult:
        lifecycle_event_id, signal_id = self._resolve_lifecycle_event(
            stored.message_id,
            revision_index,
        )
        if lifecycle_event_id is None or signal_id is None:
            self._audit_failure(
                entity_id=stored.message_id,
                entity_type="message",
                error_code="day28_lifecycle_event_not_resolved",
                decision=stored.decision,
                action=stored.action,
            )
            return Day28RouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                error_code="day28_lifecycle_event_not_resolved",
                reason="day28_lifecycle_event_not_resolved",
            )

        lock = self._locks.setdefault(f"event:{lifecycle_event_id}", asyncio.Lock())
        async with lock:
            try:
                result = await self._management.execute_owner_demo_event(
                    owner_user_id=self._owner_user_id,
                    lifecycle_event_id=lifecycle_event_id,
                )
            except Day27ManagementError as exc:
                self._audit_failure(
                    entity_id=signal_id,
                    entity_type="signal",
                    error_code=exc.code,
                    decision=stored.decision,
                    action=stored.action,
                    extra={"lifecycle_event_id": str(lifecycle_event_id)},
                )
                return Day28RouteResult(
                    outcome="blocked",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    lifecycle_event_id=lifecycle_event_id,
                    error_code=exc.code,
                    reason=exc.code,
                )

            if not result.already_applied:
                self._audit_success(
                    entity_id=signal_id,
                    entity_type="signal",
                    payload={
                        "route": "trade_update",
                        "source_revision_index": revision_index,
                        "lifecycle_event_id": str(lifecycle_event_id),
                        "actions_requested": result.actions_requested,
                        "broker_actions_sent": result.broker_actions_sent,
                        "positions_closed": result.positions_closed,
                        "positions_modified": result.positions_modified,
                        "orders_cancelled": result.orders_cancelled,
                        "automatic_execution": True,
                    },
                )

            return Day28RouteResult(
                outcome=("already_applied" if result.already_applied else "managed"),
                decision=stored.decision,
                action=stored.action,
                signal_id=signal_id,
                lifecycle_event_id=lifecycle_event_id,
                broker_actions_sent=result.broker_actions_sent,
                already_applied=result.already_applied,
                reason=(
                    "day28_management_already_applied"
                    if result.already_applied
                    else "day28_management_applied"
                ),
            )

    def _load_stored_decision(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
    ) -> _StoredDecision | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT m.id AS message_id, d.decision, d.action, d.reason
                    FROM messages AS m
                    JOIN sources AS s ON s.id = m.source_id
                    JOIN ai_message_decisions AS d
                      ON d.message_id = m.id
                     AND d.revision_index = :revision_index
                    WHERE m.source_id = :source_id
                      AND m.telegram_message_id = :telegram_message_id
                      AND m.deleted_at IS NULL
                      AND s.status IN ('testing', 'live')
                    LIMIT 1
                    """
                ),
                {
                    "source_id": source_id,
                    "telegram_message_id": telegram_message_id,
                    "revision_index": revision_index,
                },
            ).mappings().first()
        if row is None:
            return None
        return _StoredDecision(
            message_id=UUID(str(row["message_id"])),
            decision=str(row["decision"] or ""),
            action=str(row["action"] or ""),
            reason=str(row["reason"] or ""),
        )

    def _resolve_signal_id(self, message_id: UUID, revision_index: int) -> UUID | None:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT id
                    FROM signals
                    WHERE source_message_id = :message_id
                      AND source_revision_index = :revision_index
                      AND parser_status = 'accepted'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"message_id": message_id, "revision_index": revision_index},
            ).scalar_one_or_none()
        return UUID(str(value)) if value is not None else None

    def _resolve_lifecycle_event(
        self,
        message_id: UUID,
        revision_index: int,
    ) -> tuple[UUID | None, UUID | None]:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, signal_id
                    FROM signal_lifecycle_events
                    WHERE source_message_id = :message_id
                      AND source_revision_index = :revision_index
                      AND origin = 'provider_update'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"message_id": message_id, "revision_index": revision_index},
            ).mappings().first()
        if row is None:
            return None, None
        return UUID(str(row["id"])), UUID(str(row["signal_id"]))

    def _has_position_records(self, signal_id: UUID) -> bool:
        return self._position_count(signal_id) > 0

    def _position_count(self, signal_id: UUID) -> int:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE signal_id = :signal_id
                      AND user_id = :user_id
                    """
                ),
                {"signal_id": signal_id, "user_id": self._owner_user_id},
            ).scalar_one()
        return int(value)

    def _audit_success(
        self,
        *,
        entity_id: UUID,
        entity_type: str,
        payload: dict[str, Any],
    ) -> None:
        # Execution outcomes were previously recorded only as audit rows. That makes
        # the single most important question about this service -- did the last
        # provider signal actually place a trade, and if not why -- answerable only
        # by querying the database. Mirror the outcome to the platform log so it is
        # observable in real time. Identifiers and counts only; no credentials,
        # balances, provider identity or message text.
        logger.info(
            "Execution route succeeded %s=%s positions=%s",
            entity_type,
            entity_id,
            payload.get("position_count", payload.get("positions", "n/a")),
        )
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.day28_route_success",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload=payload,
                )
            )
            session.commit()

    def _audit_failure(
        self,
        *,
        entity_id: UUID,
        entity_type: str,
        error_code: str,
        decision: str,
        action: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "error_code": error_code,
            "decision": decision,
            "action": action,
            "automatic_retry": False,
            "duplicate_broker_action": False,
        }
        if extra:
            payload.update(extra)
        # A skip is the normal, safe outcome for most provider messages, so this is
        # not an error-level event. It is logged because the reason code is the only
        # way to tell "correctly ignored chatter" apart from "a real trade was
        # blocked by a gate", which is exactly what an operator needs to see.
        logger.info(
            "Execution route did not place a trade %s=%s reason=%s decision=%s action=%s",
            entity_type,
            entity_id,
            error_code,
            decision,
            action,
        )
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.day28_route_failure",
                    entity_type=entity_type,
                    entity_id=entity_id,
                    payload=payload,
                )
            )
            session.commit()
