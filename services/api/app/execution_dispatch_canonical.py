"""Single canonical provider-decision -> broker dispatcher.

Production no longer composes day-numbered router generations or a duplicate static
source allow-list. A durable decision is eligible only when its source row is currently
``testing``, ``shadow`` or ``live`` in PostgreSQL. The Owner paper reference account and ordinary
member accounts are attempted independently; member LIVE mutation remains controlled by
the distribution switch outside the trading-policy engine.

Existing ``mt5.day28_*``/``mt5.day38_*`` audit event names are retained only as durable
DB compatibility/idempotency keys. Renaming those persisted keys requires a deliberate
data migration and must never make an already-executed signal look new.

A failed provider revision does not permanently poison a later corrected provider edit.
A newer revision may be attempted only when the earlier failure was non-ambiguous and no
broker-linked local artifact exists. Ambiguous MetaAPI mutation failures never trigger an
automatic broker retry.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.member_routing_canonical import MemberDistributionService, MemberManagementService
from app.models import AuditEvent
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_management_day27 import Day27ManagementError
from app.provider_execution_probation import (
    ProbationCheck,
    check_probation_eligibility,
    is_active_probation,
)
from app.shadow_trading import ShadowTradeService

logger = logging.getLogger(__name__)
_ALLOWED_RISK = {Decimal("0.5"), Decimal("1"), Decimal("1.5"), Decimal("2")}
_MANAGEMENT_RETRY_ATTEMPTS = 3
_MANAGEMENT_RETRY_DELAY_SECONDS = 3.0
_AMBIGUOUS_EXECUTION_ERRORS = {
    "metaapi_timeout",
    "metaapi_unreachable",
    "metaapi_temporarily_unavailable",
    "day26_partial_execution_rollback_failed",
    "critical_partial_execution_rollback_failed",
}


@dataclass(frozen=True, slots=True)
class CanonicalRouteResult:
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
class StoredDecision:
    message_id: UUID
    decision: str
    action: str
    reason: str
    source_status: str = "testing"
    side: str | None = None


class CanonicalExecutionDispatcher:
    """Route one durable canonical decision through shared paper/future-LIVE policy."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        owner_user_id: UUID,
        execution_service: Any,
        management_service: Any,
        member_distribution: MemberDistributionService,
        member_management: MemberManagementService,
        risk_percent: Decimal | str = Decimal("1"),
        double_lot_approved: bool = True,
    ) -> None:
        risk = Decimal(str(risk_percent))
        if risk not in _ALLOWED_RISK:
            raise ValueError("canonical_risk_percent_invalid")
        self._session_factory = session_factory
        self._owner_user_id = owner_user_id
        self._execution = execution_service
        self._management = management_service
        self._member_distribution = member_distribution
        self._member_management = member_management
        self._risk_percent = risk
        self._double_lot_approved = bool(double_lot_approved)
        self._shadow = ShadowTradeService(session_factory)
        self._locks: dict[str, asyncio.Lock] = {}

    async def dispatch_stored_decision(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int = 0,
    ) -> CanonicalRouteResult:
        stored = self._load_stored_decision(
            source_id=source_id,
            telegram_message_id=telegram_message_id,
            revision_index=revision_index,
        )
        if stored is None:
            logger.info(
                "Message ignored source=%s telegram_message_id=%s reason=%s",
                source_id,
                telegram_message_id,
                "source_not_testing_or_live_or_decision_missing",
            )
            return CanonicalRouteResult(
                outcome="blocked",
                decision=None,
                action=None,
                error_code="stored_decision_missing",
                reason="source_not_testing_or_live_or_decision_missing",
            )

        if stored.source_status == "shadow":
            return self._dispatch_shadow(stored, revision_index)

        if stored.decision == "new_trade" and stored.action == "execute":
            if self._owner_shadow_only_new_trade(source_id):
                logger.info(
                    "New trade shadow-only by owner policy source=%s telegram_message_id=%s",
                    source_id,
                    telegram_message_id,
                )
                return self._dispatch_shadow(
                    stored,
                    revision_index,
                    reason_override="owner_shadow_only_scalper_or_trade_global",
                )
            probation = self._check_probation(source_id, stored.side)
            if not probation.eligible:
                logger.info(
                    "New trade held on probation source=%s telegram_message_id=%s reason=%s",
                    source_id,
                    telegram_message_id,
                    probation.reason,
                )
                return self._dispatch_shadow(
                    stored, revision_index, reason_override=probation.reason
                )
            logger.info(
                "Dispatching new trade source=%s telegram_message_id=%s revision=%s",
                source_id,
                telegram_message_id,
                revision_index,
            )
            return await self._dispatch_new_trade(stored, revision_index, source_id=source_id)

        if stored.decision == "trade_update" and stored.action == "apply_update":
            logger.info(
                "Dispatching management update source=%s telegram_message_id=%s revision=%s",
                source_id,
                telegram_message_id,
                revision_index,
            )
            return await self._dispatch_management(stored, revision_index)

        logger.info(
            "Message ignored source=%s telegram_message_id=%s decision=%s action=%s reason=%s",
            source_id,
            telegram_message_id,
            stored.decision,
            stored.action,
            stored.reason,
        )
        return CanonicalRouteResult(
            outcome="ignored",
            decision=stored.decision,
            action=stored.action,
            reason=stored.reason,
        )

    def _owner_shadow_only_new_trade(self, source_id: UUID) -> bool:
        """Owner policy: scalpers and TRADE GLOBAL are research/shadow only.

        This applies only to NEW entries. Existing broker positions still receive their
        provider's management/close instructions until they are flat.
        """
        session_factory = getattr(self, "_session_factory", None)
        if session_factory is None:
            return False
        context = session_factory()
        if context is None:
            return False
        with context as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        COALESCE(s.source_alias,s.chat_title,'') AS provider_name,
                        COALESCE(pr.style,'unknown') AS style,
                        COALESCE(
                            pr.profile_metadata->'adaptive_v1'->'language'->>'cadence_bucket',
                            'unknown'
                        ) AS cadence
                    FROM sources s
                    LEFT JOIN provider_research_profiles pr ON pr.source_id=s.id
                    WHERE s.id=:source_id
                    """
                ),
                {"source_id": source_id},
            ).mappings().first()
        if row is None:
            return False
        provider = str(row["provider_name"] or "").strip().casefold()
        style = str(row["style"] or "").strip().casefold()
        cadence = str(row["cadence"] or "").strip().casefold()
        return provider == "trade global" or style == "scalper" or cadence == "scalper"

    def _check_probation(self, source_id: UUID, side: str | None) -> ProbationCheck:
        with self._session_factory() as session:
            return check_probation_eligibility(session, source_id=source_id, side=side)

    def _is_active_probation(self, source_id: UUID) -> bool:
        with self._session_factory() as session:
            return is_active_probation(session, source_id=source_id)

    @staticmethod
    async def _execute_management_with_retry(
        service: Any,
        *,
        owner_user_id: UUID,
        lifecycle_event_id: UUID,
    ) -> tuple[Any | None, str | None]:
        """Retry only errors the gateway itself marked retryable (timeouts, stale link).

        A genuinely ambiguous match (unresolved layer/price target) is never marked
        retryable and is never retried here -- repeated identical failures on that same
        message are instead picked up by ``management_reliability_runtime`` and, after a
        bounded number of attempts, force-closed rather than left open indefinitely.
        """
        last_error: str | None = None
        for attempt in range(_MANAGEMENT_RETRY_ATTEMPTS):
            try:
                result = await service.execute_owner_demo_event(
                    owner_user_id=owner_user_id,
                    lifecycle_event_id=lifecycle_event_id,
                )
            except Day27ManagementError as exc:
                last_error = exc.code
                if not exc.retryable or attempt == _MANAGEMENT_RETRY_ATTEMPTS - 1:
                    return None, last_error
                await asyncio.sleep(_MANAGEMENT_RETRY_DELAY_SECONDS)
                continue
            return result, None
        return None, last_error

    def _dispatch_shadow(
        self,
        stored: StoredDecision,
        revision_index: int,
        *,
        reason_override: str | None = None,
    ) -> CanonicalRouteResult:
        """Persist isolated virtual state without invoking broker/member services.

        Also used to hold back a `testing`/`live` provider's signal that is still on
        probation and did not match its own best-side evidence -- recorded exactly like
        a shadow signal, just tagged with why it was held rather than dispatched.
        """
        if stored.decision == "new_trade" and stored.action == "execute":
            signal_id = self._resolve_signal_id(stored.message_id, revision_index)
            recorded = signal_id is not None and self._shadow.record_signal(signal_id)
            default_reason = "shadow_signal_recorded" if recorded else "shadow_signal_not_eligible"
            return CanonicalRouteResult(
                outcome="shadowed" if recorded else "ignored",
                decision=stored.decision, action=stored.action, signal_id=signal_id,
                reason=reason_override or default_reason,
            )
        if stored.decision == "trade_update" and stored.action == "apply_update":
            event_id, signal_id = self._resolve_lifecycle_event(stored.message_id, revision_index)
            recorded = event_id is not None and self._shadow.record_management(event_id)
            return CanonicalRouteResult(
                outcome="shadowed" if recorded else "ignored",
                decision=stored.decision, action=stored.action, signal_id=signal_id,
                lifecycle_event_id=event_id,
                reason="shadow_management_recorded" if recorded else "shadow_management_not_applicable",
            )
        return CanonicalRouteResult(
            outcome="ignored", decision=stored.decision, action=stored.action, reason=stored.reason
        )

    def _unresolved_management_after_signal(
        self,
        *,
        signal_id: UUID,
        source_id: UUID,
    ) -> dict[str, object] | None:
        """Find provider management that arrived after a signal but before broker entry.

        This closes a real race: a missed/recovered full signal can finish AI processing
        after a later TP/SL/close update has already been understood. If that update could
        not link because the signal did not exist yet, opening the original untouched trade
        is stale and unsafe. Fail closed instead of entering after the provider has already
        changed the trade.
        """
        session_factory = getattr(self, "_session_factory", None)
        if session_factory is None:
            return None
        session_context = session_factory()
        if session_context is None:
            return None
        with session_context as session:
            row = session.execute(
                text(
                    """
                    SELECT m.id,m.telegram_message_id,m.posted_at,d.reason
                    FROM signals s
                    JOIN messages m ON m.source_id=s.source_id
                    JOIN ai_message_decisions d
                      ON d.message_id=m.id AND d.revision_index=0
                    WHERE s.id=:signal_id
                      AND s.source_id=:source_id
                      AND m.posted_at>s.source_posted_at
                      AND d.decision='trade_update'
                      AND d.action='apply_update'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM signal_lifecycle_events e
                          WHERE e.source_message_id=m.id
                      )
                    ORDER BY m.posted_at,m.telegram_message_id
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id, "source_id": source_id},
            ).mappings().first()
        return dict(row) if row is not None else None

    async def _dispatch_new_trade(
        self,
        stored: StoredDecision,
        revision_index: int,
        *,
        source_id: UUID,
    ) -> CanonicalRouteResult:
        signal_id = self._resolve_signal_id(stored.message_id, revision_index)
        if signal_id is None:
            self._audit_failure(
                entity_id=stored.message_id,
                entity_type="message",
                error_code="signal_not_resolved",
                decision=stored.decision,
                action=stored.action,
            )
            return CanonicalRouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                error_code="signal_not_resolved",
                reason="signal_not_resolved",
            )

        lock = self._locks.setdefault(f"signal:{signal_id}", asyncio.Lock())
        async with lock:
            unresolved = self._unresolved_management_after_signal(
                signal_id=signal_id,
                source_id=source_id,
            )
            if unresolved is not None:
                error_code = "provider_updated_before_delayed_execution"
                self._audit_failure(
                    entity_id=signal_id,
                    entity_type="signal",
                    error_code=error_code,
                    decision=stored.decision,
                    action=stored.action,
                    extra={
                        "source_revision_index": revision_index,
                        "provider_management_message_id": str(unresolved.get("id") or ""),
                        "provider_telegram_message_id": unresolved.get("telegram_message_id"),
                        "provider_management_posted_at": str(unresolved.get("posted_at") or ""),
                        "provider_management_reason": unresolved.get("reason"),
                        "broker_mutation_created": False,
                    },
                )
                self._audit_new_trade_route(
                    signal_id=signal_id,
                    outcome="blocked",
                    position_count=0,
                    error_code=error_code,
                )
                return CanonicalRouteResult(
                    outcome="blocked",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    error_code=error_code,
                    reason=error_code,
                )

            prior = self._prior_new_trade_route(signal_id)
            if prior is not None:
                if prior.get("outcome") == "executed":
                    return CanonicalRouteResult(
                        outcome="already_applied",
                        decision=stored.decision,
                        action=stored.action,
                        signal_id=signal_id,
                        position_count=int(prior.get("position_count") or 0),
                        already_applied=True,
                        reason="distribution_already_attempted",
                    )

                error = str(prior.get("error_code") or "distribution_already_attempted")
                if self._prior_failure_blocks_revision(prior, revision_index):
                    return CanonicalRouteResult(
                        outcome="blocked",
                        decision=stored.decision,
                        action=stored.action,
                        signal_id=signal_id,
                        error_code=error,
                        reason=error,
                    )

                if not self._clear_unmapped_failed_owner_plans(signal_id):
                    # A prior attempt left broker-linked or otherwise non-disposable
                    # state. Never turn a newer Telegram edit into a duplicate mutation.
                    return CanonicalRouteResult(
                        outcome="blocked",
                        decision=stored.decision,
                        action=stored.action,
                        signal_id=signal_id,
                        error_code="prior_execution_state_requires_reconciliation",
                        reason="prior_execution_state_requires_reconciliation",
                    )

            owner_position_count = self._position_count(signal_id)
            owner_succeeded = owner_position_count > 0
            owner_error: str | None = None
            owner_double_lot_applied = False
            if not owner_succeeded:
                try:
                    owner_result = await self._execution.execute_owner_demo_signal(
                        owner_user_id=self._owner_user_id,
                        signal_id=signal_id,
                        risk_percent=str(self._risk_percent),
                        double_lot_approved=self._double_lot_approved,
                    )
                    owner_position_count = len(owner_result.positions)
                    owner_double_lot_applied = bool(owner_result.double_lot_applied)
                    owner_succeeded = owner_position_count > 0
                except Day26ExecutionError as exc:
                    owner_error = exc.code

            try:
                member_result = await self._member_distribution.distribute(
                    signal_id=signal_id,
                    exclude_live=self._is_active_probation(source_id),
                )
            except Exception:
                logger.exception("Member trade distribution failed unexpectedly")
                member_result = None

            member_executed = member_result.executed_count if member_result is not None else 0
            member_skipped = member_result.skipped_count if member_result is not None else 0
            success = owner_succeeded or member_executed > 0
            canonical_position_count = owner_position_count
            if canonical_position_count == 0 and member_result is not None:
                canonical_position_count = max(
                    (
                        item.position_count
                        for item in member_result.outcomes
                        if item.outcome == "executed"
                    ),
                    default=0,
                )

            if success:
                self._audit_success(
                    entity_id=signal_id,
                    entity_type="signal",
                    payload={
                        "route": "new_trade",
                        "source_revision_index": revision_index,
                        "risk_percent": str(self._risk_percent),
                        "double_lot_approved": self._double_lot_approved,
                        "double_lot_applied": owner_double_lot_applied,
                        "position_count": canonical_position_count,
                        "automatic_execution": True,
                        "multi_user": True,
                        "owner_reference_executed": owner_succeeded,
                        "owner_reference_error_code": owner_error,
                        "member_target_count": (
                            member_result.target_count if member_result is not None else None
                        ),
                        "member_executed_count": member_executed,
                        "member_skipped_count": member_skipped,
                        "owner_failure_blocked_members": False,
                    },
                )
                self._audit_new_trade_route(
                    signal_id=signal_id,
                    outcome="executed",
                    position_count=canonical_position_count,
                    error_code=None,
                )
                return CanonicalRouteResult(
                    outcome="executed",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    position_count=canonical_position_count,
                    reason="multi_user_trade_executed",
                )

            error_code = owner_error or "no_user_execution_succeeded"
            self._audit_failure(
                entity_id=signal_id,
                entity_type="signal",
                error_code=error_code,
                decision=stored.decision,
                action=stored.action,
                extra={
                    "source_revision_index": revision_index,
                    "multi_user": True,
                    "member_target_count": (
                        member_result.target_count if member_result is not None else None
                    ),
                    "member_executed_count": member_executed,
                    "member_skipped_count": member_skipped,
                    "owner_failure_blocked_members": False,
                },
            )
            self._audit_new_trade_route(
                signal_id=signal_id,
                outcome="blocked",
                position_count=0,
                error_code=error_code,
            )
            return CanonicalRouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                signal_id=signal_id,
                error_code=error_code,
                reason=error_code,
            )

    async def _dispatch_management(
        self,
        stored: StoredDecision,
        revision_index: int,
    ) -> CanonicalRouteResult:
        lifecycle_event_id, signal_id = self._resolve_lifecycle_event(
            stored.message_id,
            revision_index,
        )
        if lifecycle_event_id is None or signal_id is None:
            self._audit_failure(
                entity_id=stored.message_id,
                entity_type="message",
                error_code="lifecycle_event_not_resolved",
                decision=stored.decision,
                action=stored.action,
            )
            return CanonicalRouteResult(
                outcome="blocked",
                decision=stored.decision,
                action=stored.action,
                error_code="lifecycle_event_not_resolved",
                reason="lifecycle_event_not_resolved",
            )

        lock = self._locks.setdefault(f"event:{lifecycle_event_id}", asyncio.Lock())
        async with lock:
            owner_has_positions = self._active_exposure_count(signal_id) > 0
            owner_result = None
            owner_error: str | None = None
            if owner_has_positions:
                owner_result, owner_error = await self._execute_management_with_retry(
                    self._management,
                    owner_user_id=self._owner_user_id,
                    lifecycle_event_id=lifecycle_event_id,
                )

            try:
                member_result = await self._member_management.distribute(
                    signal_id=signal_id,
                    lifecycle_event_id=lifecycle_event_id,
                )
            except Exception:
                logger.exception("Member management distribution failed unexpectedly")
                member_result = None

            no_exposure_anywhere = (
                not owner_has_positions
                and member_result is not None
                and member_result.target_count == 0
            )
            if no_exposure_anywhere:
                self._audit_success(
                    entity_id=signal_id,
                    entity_type="signal",
                    payload={
                        "route": "trade_update",
                        "source_revision_index": revision_index,
                        "lifecycle_event_id": str(lifecycle_event_id),
                        "broker_actions_sent": 0,
                        "automatic_execution": True,
                        "multi_user": True,
                        "management_applicable": False,
                        "no_mapped_exposure": True,
                        "owner_reference_managed": False,
                        "owner_reference_error_code": None,
                        "member_target_count": 0,
                        "member_managed_count": 0,
                        "member_skipped_count": 0,
                        "owner_failure_blocked_members": False,
                    },
                )
                return CanonicalRouteResult(
                    outcome="ignored",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    lifecycle_event_id=lifecycle_event_id,
                    broker_actions_sent=0,
                    reason="management_not_applicable_no_positions",
                )

            member_success = (
                member_result.any_management_succeeded if member_result is not None else False
            )
            owner_success = owner_result is not None
            if not owner_success and not member_success:
                error_code = owner_error or "no_user_management_succeeded"
                self._audit_failure(
                    entity_id=signal_id,
                    entity_type="signal",
                    error_code=error_code,
                    decision=stored.decision,
                    action=stored.action,
                    extra={
                        "source_revision_index": revision_index,
                        "lifecycle_event_id": str(lifecycle_event_id),
                        "multi_user": True,
                        "owner_failure_blocked_members": False,
                    },
                )
                return CanonicalRouteResult(
                    outcome="blocked",
                    decision=stored.decision,
                    action=stored.action,
                    signal_id=signal_id,
                    lifecycle_event_id=lifecycle_event_id,
                    error_code=error_code,
                    reason=error_code,
                )

            broker_actions = owner_result.broker_actions_sent if owner_result is not None else 0
            already_applied = bool(owner_result is not None and owner_result.already_applied)
            if member_result is not None:
                broker_actions += sum(
                    item.broker_actions_sent for item in member_result.outcomes
                )
                if member_result.target_count:
                    already_applied = already_applied and (
                        member_result.already_applied_count == member_result.target_count
                    )

            self._audit_success(
                entity_id=signal_id,
                entity_type="signal",
                payload={
                    "route": "trade_update",
                    "source_revision_index": revision_index,
                    "lifecycle_event_id": str(lifecycle_event_id),
                    "broker_actions_sent": broker_actions,
                    "automatic_execution": True,
                    "multi_user": True,
                    "owner_reference_managed": owner_success,
                    "owner_reference_error_code": owner_error,
                    "member_target_count": (
                        member_result.target_count if member_result is not None else None
                    ),
                    "member_managed_count": (
                        member_result.managed_count if member_result is not None else 0
                    ),
                    "member_skipped_count": (
                        member_result.skipped_count if member_result is not None else 0
                    ),
                    "owner_failure_blocked_members": False,
                },
            )
            return CanonicalRouteResult(
                outcome=("already_applied" if already_applied else "managed"),
                decision=stored.decision,
                action=stored.action,
                signal_id=signal_id,
                lifecycle_event_id=lifecycle_event_id,
                broker_actions_sent=broker_actions,
                already_applied=already_applied,
                reason=(
                    "management_already_applied"
                    if already_applied
                    else "multi_user_management_applied"
                ),
            )

    def _load_stored_decision(
        self,
        *,
        source_id: UUID,
        telegram_message_id: int,
        revision_index: int,
    ) -> StoredDecision | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT m.id AS message_id, d.decision, d.action, d.reason,
                           s.status AS source_status, d.extracted->>'side' AS side
                    FROM messages AS m
                    JOIN sources AS s ON s.id=m.source_id
                    JOIN ai_message_decisions AS d
                      ON d.message_id=m.id
                     AND d.revision_index=:revision_index
                    WHERE m.source_id=:source_id
                      AND m.telegram_message_id=:telegram_message_id
                      AND m.deleted_at IS NULL
                      AND s.status IN ('testing','shadow','live')
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
        return StoredDecision(
            message_id=UUID(str(row["message_id"])),
            decision=str(row["decision"] or ""),
            action=str(row["action"] or ""),
            reason=str(row["reason"] or ""),
            source_status=str(row["source_status"] or ""),
            side=(str(row["side"]) if row["side"] else None),
        )

    def _resolve_signal_id(self, message_id: UUID, revision_index: int) -> UUID | None:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT id
                    FROM signals
                    WHERE source_message_id=:message_id
                      AND source_revision_index=:revision_index
                      AND parser_status='accepted'
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
                    WHERE source_message_id=:message_id
                      AND source_revision_index=:revision_index
                      AND origin='provider_update'
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
        """Count only broker-backed execution truth, never planned/error debris."""
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE signal_id=:signal_id
                      AND user_id=:user_id
                      AND (
                        (status='pending' AND broker_order_id IS NOT NULL)
                        OR (status='open' AND broker_position_id IS NOT NULL)
                        OR (
                            status='closed'
                            AND broker_position_id IS NOT NULL
                            AND COALESCE(close_reason,'') NOT ILIKE '%rollback%'
                        )
                      )
                    """
                ),
                {"signal_id": signal_id, "user_id": self._owner_user_id},
            ).scalar_one()
        return int(value)

    def _active_exposure_count(self, signal_id: UUID) -> int:
        """Count only broker-backed positions/orders which can still be managed."""
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE signal_id=:signal_id
                      AND user_id=:user_id
                      AND (
                        (status='pending' AND broker_order_id IS NOT NULL)
                        OR (status='open' AND broker_position_id IS NOT NULL)
                      )
                    """
                ),
                {"signal_id": signal_id, "user_id": self._owner_user_id},
            ).scalar_one()
        return int(value)

    @staticmethod
    def _prior_failure_blocks_revision(prior: dict[str, Any], revision_index: int) -> bool:
        error = str(prior.get("error_code") or "")
        if error in _AMBIGUOUS_EXECUTION_ERRORS:
            return True
        try:
            prior_revision = int(prior.get("source_revision_index"))
        except (TypeError, ValueError):
            # A legacy route record does not contain enough evidence to prove a new
            # attempt is distinct. Fail closed rather than reinterpret old state.
            return True
        return prior_revision >= revision_index

    def _clear_unmapped_failed_owner_plans(self, signal_id: UUID) -> bool:
        """Discard only broker-free error rows before a newer provider revision.

        The route-failure audit remains immutable. If any row is broker-linked, pending,
        open, closed or otherwise non-error, the newer revision is blocked for explicit
        reconciliation instead of risking a duplicate trade.
        """
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id,status,broker_order_id,broker_position_id
                    FROM positions
                    WHERE signal_id=:signal_id AND user_id=:user_id
                    """
                ),
                {"signal_id": signal_id, "user_id": self._owner_user_id},
            ).mappings().all()
            if not rows:
                return True
            disposable = all(
                str(row["status"] or "") == "error"
                and not str(row["broker_order_id"] or "").strip()
                and not str(row["broker_position_id"] or "").strip()
                for row in rows
            )
            if not disposable:
                return False
            session.execute(
                text(
                    """
                    DELETE FROM positions
                    WHERE signal_id=:signal_id
                      AND user_id=:user_id
                      AND status='error'
                      AND broker_order_id IS NULL
                      AND broker_position_id IS NULL
                    """
                ),
                {"signal_id": signal_id, "user_id": self._owner_user_id},
            )
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.canonical_failed_plan_cleared_for_provider_revision",
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={
                        "rows_cleared": len(rows),
                        "broker_mutation_present": False,
                        "automatic_retry": False,
                    },
                )
            )
            session.commit()
        return True

    def _prior_new_trade_route(self, signal_id: UUID) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT payload
                    FROM audit_events
                    WHERE event_type='mt5.day38_route_new_trade'
                      AND entity_type='signal'
                      AND entity_id=:signal_id
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).scalar_one_or_none()
        return row if isinstance(row, dict) else None

    def _audit_new_trade_route(
        self,
        *,
        signal_id: UUID,
        outcome: str,
        position_count: int,
        error_code: str | None,
    ) -> None:
        with self._session_factory() as session:
            revision_index = session.execute(
                text("SELECT source_revision_index FROM signals WHERE id=:signal_id LIMIT 1"),
                {"signal_id": signal_id},
            ).scalar_one_or_none()
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.day38_route_new_trade",
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={
                        "outcome": outcome,
                        "position_count": position_count,
                        "error_code": error_code,
                        "source_revision_index": (
                            int(revision_index) if revision_index is not None else None
                        ),
                        "automatic_retry": False,
                    },
                )
            )
            session.commit()

    def _audit_success(
        self,
        *,
        entity_id: UUID,
        entity_type: str,
        payload: dict[str, Any],
    ) -> None:
        positions = payload.get("position_count", payload.get("positions", 0))
        broker_actions = payload.get(
            "broker_actions_sent", payload.get("broker_actions", 0)
        )
        logger.info(
            "Execution route completed %s=%s positions=%s broker_actions=%s",
            entity_type,
            entity_id,
            positions,
            broker_actions,
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


__all__ = ["CanonicalExecutionDispatcher", "CanonicalRouteResult", "StoredDecision"]