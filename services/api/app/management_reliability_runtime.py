"""Recover stuck provider management instructions, then fail safe if they stay stuck.

Root cause this exists for: when a provider's "close"/"move to breakeven"/"partial"
instruction cannot be matched to a broker position (an ambiguous layer/price target, a
stale broker read, a transient gateway error), ``CanonicalExecutionDispatcher`` records
one ``mt5.day28_route_failure`` audit event and moves on -- nothing ever retries it and
the position stays open and unprotected indefinitely. That silent gap is the confirmed
root cause of TIG's Asia Trades real losses (see the 4 September and 9 September incident
scripts): the position kept running unmanaged until the broker itself stopped it out.

This runtime closes that gap without changing how a management instruction is
interpreted:

1. Every sweep, it finds ``trade_update`` decisions whose most recent outcome is still a
   recorded failure (no later success, no prior fail-safe close) and simply re-dispatches
   them through the exact same canonical path. Most of the previously-lost instructions
   were transient (a stale broker read, a timed-out gateway call) and now succeed on
   retry with the ordinary, fully-tested logic -- nothing here reinterprets a message.
2. Only once a specific instruction has failed ``_MAX_ATTEMPTS`` times AND the signal
   still has a broker-confirmed open position for some user does it escalate: every user
   still holding exposure on that signal has their position force-closed via
   ``Day27Mt5ManagementService.force_close_all_positions`` -- the same unambiguous
   "close everything" outcome an ordinary full close already reaches, just triggered
   directly. Getting flat beats leaving real (or paper) money exposed with a protective
   instruction the system has already proven, repeatedly, it cannot apply.

Every escalation is recorded as its own audit event so it is never repeated for the same
instruction and so a human (or AIDY) can see exactly what was force-closed and why.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.execution_dispatch_canonical import CanonicalExecutionDispatcher
from app.models import AuditEvent
from app.mt5_management_day27 import Day27ManagementError

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 30
_LOOKBACK_INTERVAL = "2 hours"
_MAX_ATTEMPTS = 4
_FAILSAFE_EVENT_TYPE = "mt5.management_failsafe_close"

_CANDIDATES_SQL = """
WITH failures AS (
    SELECT
        ae.entity_id AS signal_id,
        (ae.payload->>'lifecycle_event_id')::uuid AS lifecycle_event_id,
        (ae.payload->>'source_revision_index')::int AS revision_index,
        ae.created_at
    FROM audit_events AS ae
    WHERE ae.event_type = 'mt5.day28_route_failure'
      AND ae.payload->>'decision' = 'trade_update'
      AND ae.payload ? 'lifecycle_event_id'
      AND COALESCE(ae.payload->>'error_code','') <> 'partial_volume_below_broker_minimum'
      AND ae.created_at >= now() - CAST(:lookback AS interval)
),
grouped AS (
    SELECT
        signal_id,
        lifecycle_event_id,
        count(*) AS failure_count,
        max(revision_index) AS revision_index,
        max(created_at) AS last_failure_at
    FROM failures
    GROUP BY signal_id, lifecycle_event_id
),
successes AS (
    SELECT
        (payload->>'lifecycle_event_id')::uuid AS lifecycle_event_id,
        max(created_at) AS last_success_at
    FROM audit_events
    WHERE event_type = 'mt5.day28_route_success'
      AND payload->>'route' = 'trade_update'
      AND payload ? 'lifecycle_event_id'
    GROUP BY 1
),
escalated AS (
    SELECT DISTINCT (payload->>'lifecycle_event_id')::uuid AS lifecycle_event_id
    FROM audit_events
    WHERE event_type = :escalated_event_type
)
SELECT g.signal_id, g.lifecycle_event_id, g.revision_index, g.failure_count
FROM grouped AS g
LEFT JOIN successes AS s ON s.lifecycle_event_id = g.lifecycle_event_id
LEFT JOIN escalated AS e ON e.lifecycle_event_id = g.lifecycle_event_id
WHERE e.lifecycle_event_id IS NULL
  AND (s.last_success_at IS NULL OR s.last_success_at < g.last_failure_at)
ORDER BY g.last_failure_at
"""

_UNATTEMPTED_SQL = """
SELECT
    e.signal_id,
    e.id AS lifecycle_event_id,
    COALESCE((e.aggregate_result->>'source_revision_index')::int, 0) AS revision_index,
    0::int AS failure_count
FROM signal_lifecycle_events AS e
JOIN messages AS m ON m.id=e.source_message_id
JOIN sources AS src ON src.id=m.source_id
WHERE e.origin='provider_update'
  AND e.created_at >= now() - CAST(:lookback AS interval)
  AND src.status IN ('testing','live')
  AND jsonb_typeof(
        COALESCE(e.aggregate_result->'revised_instruction'->'management_actions','[]'::jsonb)
      )='array'
  AND jsonb_array_length(
        COALESCE(e.aggregate_result->'revised_instruction'->'management_actions','[]'::jsonb)
      ) > 0
  AND EXISTS (
      SELECT 1
      FROM positions AS p
      WHERE p.signal_id=e.signal_id
        AND (
          (p.status='open' AND p.broker_position_id IS NOT NULL)
          OR (p.status='pending' AND p.broker_order_id IS NOT NULL)
        )
  )
  AND NOT EXISTS (
      SELECT 1
      FROM audit_events AS ae
      WHERE ae.event_type IN ('mt5.day28_route_success','mt5.day28_route_failure')
        AND ae.payload->>'lifecycle_event_id'=e.id::text
  )
  AND NOT EXISTS (
      SELECT 1
      FROM audit_events AS ae
      WHERE ae.event_type=:escalated_event_type
        AND ae.payload->>'lifecycle_event_id'=e.id::text
  )
ORDER BY e.created_at
"""

_SOURCE_MESSAGE_SQL = """
SELECT m.source_id, m.telegram_message_id
FROM signal_lifecycle_events AS e
JOIN messages AS m ON m.id = e.source_message_id
WHERE e.id = :lifecycle_event_id
LIMIT 1
"""

_OPEN_POSITION_USERS_SQL = """
SELECT DISTINCT p.user_id
FROM positions AS p
WHERE p.signal_id = :signal_id AND p.status = 'open'
"""

_ACCOUNT_ENVIRONMENT_SQL = """
SELECT lower(account_environment) AS account_environment
FROM mt5_accounts
WHERE owner_user_id = :user_id AND status != 'revoked'
LIMIT 1
"""


class ManagementReliabilityRuntime:
    """Retry stuck management instructions, then force-flat exposure that never resolves."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        dispatcher: CanonicalExecutionDispatcher,
        demo_management: Any,
        live_management: Any,
        owner_user_id: UUID,
        interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._dispatcher = dispatcher
        self._demo_management = demo_management
        self._live_management = live_management
        self._owner_user_id = owner_user_id
        self._interval_seconds = interval_seconds
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(
                self._run(), name="management-reliability-runtime"
            )

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.sweep_once()
            except Exception:
                logger.exception("Management reliability sweep failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                pass

    async def sweep_once(self) -> None:
        for candidate in self._find_candidates():
            await self._handle(candidate)

    def _find_candidates(self) -> list[dict[str, Any]]:
        params = {
            "lookback": _LOOKBACK_INTERVAL,
            "escalated_event_type": _FAILSAFE_EVENT_TYPE,
        }
        with self._session_factory() as session:
            unattempted = session.execute(
                text(_UNATTEMPTED_SQL),
                params,
            ).mappings().all()
            failed = session.execute(
                text(_CANDIDATES_SQL),
                params,
            ).mappings().all()
        # Unattempted first: these are the dangerous restart/deploy handoff gap where
        # interpretation succeeded but broker dispatch never ran.
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for row in [*unattempted, *failed]:
            item = dict(row)
            key = str(item["lifecycle_event_id"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
        return rows

    async def _handle(self, candidate: dict[str, Any]) -> None:
        signal_id: UUID = candidate["signal_id"]
        lifecycle_event_id: UUID = candidate["lifecycle_event_id"]
        failure_count: int = int(candidate["failure_count"])

        if failure_count < _MAX_ATTEMPTS:
            await self._retry(candidate)
            return

        await self._escalate(signal_id=signal_id, lifecycle_event_id=lifecycle_event_id, failure_count=failure_count)

    async def _retry(self, candidate: dict[str, Any]) -> None:
        with self._session_factory() as session:
            source_row = session.execute(
                text(_SOURCE_MESSAGE_SQL),
                {"lifecycle_event_id": candidate["lifecycle_event_id"]},
            ).mappings().first()
        if source_row is None:
            return
        logger.info(
            "Retrying stuck management instruction signal=%s lifecycle_event=%s attempt=%s",
            candidate["signal_id"],
            candidate["lifecycle_event_id"],
            int(candidate["failure_count"]) + 1,
        )
        await self._dispatcher.dispatch_stored_decision(
            source_id=source_row["source_id"],
            telegram_message_id=int(source_row["telegram_message_id"]),
            revision_index=int(candidate["revision_index"]),
        )

    async def _escalate(
        self,
        *,
        signal_id: UUID,
        lifecycle_event_id: UUID,
        failure_count: int,
    ) -> None:
        with self._session_factory() as session:
            user_ids = [
                row["user_id"]
                for row in session.execute(
                    text(_OPEN_POSITION_USERS_SQL), {"signal_id": signal_id}
                ).mappings().all()
            ]
        if not user_ids:
            self._audit_escalation(
                signal_id=signal_id,
                lifecycle_event_id=lifecycle_event_id,
                failure_count=failure_count,
                closed_by_user={},
                skipped_users=[],
            )
            return

        closed_by_user: dict[str, int] = {}
        skipped_users: list[dict[str, str]] = []
        for user_id in user_ids:
            service = self._management_service_for(user_id)
            if service is None:
                skipped_users.append({"user_id": str(user_id), "reason": "account_environment_unresolved"})
                continue
            try:
                closed = await service.force_close_all_positions(
                    owner_user_id=user_id, signal_id=signal_id
                )
            except Day27ManagementError as exc:
                logger.error(
                    "Fail-safe close failed signal=%s user=%s reason=%s",
                    signal_id,
                    user_id,
                    exc.code,
                )
                skipped_users.append({"user_id": str(user_id), "reason": exc.code})
                continue
            closed_by_user[str(user_id)] = closed

        logger.error(
            "Fail-safe closed unresolved management exposure signal=%s "
            "lifecycle_event=%s failure_count=%s closed=%s skipped=%s",
            signal_id,
            lifecycle_event_id,
            failure_count,
            closed_by_user,
            skipped_users,
        )
        self._audit_escalation(
            signal_id=signal_id,
            lifecycle_event_id=lifecycle_event_id,
            failure_count=failure_count,
            closed_by_user=closed_by_user,
            skipped_users=skipped_users,
        )

    def _management_service_for(self, user_id: UUID) -> Any | None:
        if user_id == self._owner_user_id:
            return self._demo_management
        with self._session_factory() as session:
            row = session.execute(
                text(_ACCOUNT_ENVIRONMENT_SQL), {"user_id": user_id}
            ).mappings().first()
        if row is None:
            return None
        environment = str(row["account_environment"] or "")
        if environment == "live":
            return self._live_management
        if environment == "demo":
            return self._demo_management
        return None

    def _audit_escalation(
        self,
        *,
        signal_id: UUID,
        lifecycle_event_id: UUID,
        failure_count: int,
        closed_by_user: dict[str, int],
        skipped_users: list[dict[str, str]],
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type=_FAILSAFE_EVENT_TYPE,
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={
                        "lifecycle_event_id": str(lifecycle_event_id),
                        "failure_count": failure_count,
                        "positions_closed_by_user": closed_by_user,
                        "skipped_users": skipped_users,
                        "reason": "management_instruction_unresolved_after_retry_budget",
                    },
                )
            )
            session.commit()


__all__ = ["ManagementReliabilityRuntime"]
