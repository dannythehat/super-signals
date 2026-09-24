"""Automatic stale-position safety watchdog.

This service closes only mapped Smart Signals positions that have exceeded a hard
maximum age. It never touches manual/unmapped broker positions. Broker closes reuse
OwnerManualCloseService, including broker-state rechecks and advisory locks.

Defaults:
- TIG's Asia Trades: 12 hours
- all other mapped providers: 24 hours

The watchdog is intentionally isolated from Uvicorn's event loop. Each broker-close
pass runs in a worker thread with its own async loop so a slow MetaAPI response cannot
starve /health, Telegram intake, or live execution.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.owner_manual_close import OwnerManualCloseError, OwnerManualCloseService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StalePositionCandidate:
    position_id: UUID
    provider: str
    opened_at: datetime
    max_age_hours: float


class StalePositionWatchdog:
    """Broker-confirmed hard age cap for mapped Smart Signals positions."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        owner_user_id: UUID,
        poll_seconds: int = 60,
        default_max_age_hours: float = 24.0,
        tig_max_age_hours: float = 12.0,
    ) -> None:
        if poll_seconds < 15:
            raise ValueError("stale_position_watchdog_poll_too_fast")
        if default_max_age_hours <= 0 or tig_max_age_hours <= 0:
            raise ValueError("stale_position_watchdog_age_invalid")
        self._session_factory = session_factory
        self._cipher = cipher
        self._owner_user_id = owner_user_id
        self._poll_seconds = poll_seconds
        self._default_max_age_hours = default_max_age_hours
        self._tig_max_age_hours = tig_max_age_hours
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def max_age_hours_for_provider(self, provider: str) -> float:
        normalized = (provider or "").casefold()
        if "tig" in normalized and "asia" in normalized:
            return self._tig_max_age_hours
        return self._default_max_age_hours

    def _candidates(self, now: datetime) -> tuple[StalePositionCandidate, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT p.id,p.opened_at,
                           COALESCE(NULLIF(src.chat_title,''),src.source_alias,'') AS provider
                    FROM positions AS p
                    JOIN signals AS sig ON sig.id=p.signal_id
                    JOIN sources AS src ON src.id=sig.source_id
                    WHERE p.user_id=:owner_user_id
                      AND p.status='open'
                      AND p.broker_position_id IS NOT NULL
                    ORDER BY p.opened_at,p.id
                    """
                ),
                {"owner_user_id": self._owner_user_id},
            ).mappings().all()

        result: list[StalePositionCandidate] = []
        for row in rows:
            opened_at = row["opened_at"]
            if opened_at is None:
                continue
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=UTC)
            else:
                opened_at = opened_at.astimezone(UTC)
            provider = str(row["provider"] or "").strip()
            max_age_hours = self.max_age_hours_for_provider(provider)
            if opened_at <= now - timedelta(hours=max_age_hours):
                result.append(
                    StalePositionCandidate(
                        position_id=row["id"],
                        provider=provider,
                        opened_at=opened_at,
                        max_age_hours=max_age_hours,
                    )
                )
        return tuple(result)

    def _close_candidate_isolated(self, position_id: UUID):
        service = OwnerManualCloseService(
            session_factory=self._session_factory,
            cipher=self._cipher,
            read_gateway=MetaApiReadGateway(),
            trade_gateway=MetaApiTradeGateway(),
        )
        return asyncio.run(
            service.close_position(
                self._owner_user_id,
                position_id,
                scope="stale_watchdog",
            )
        )

    async def poll_once(self) -> int:
        now = datetime.now(UTC)
        candidates = await asyncio.to_thread(self._candidates, now)
        closed_count = 0
        for candidate in candidates:
            age_hours = max(0.0, (now - candidate.opened_at).total_seconds() / 3600.0)
            try:
                result = await asyncio.to_thread(
                    self._close_candidate_isolated,
                    candidate.position_id,
                )
            except OwnerManualCloseError as exc:
                if exc.code == "owner_manual_position_not_open":
                    continue
                logger.error(
                    "Stale position watchdog close failed position=%s provider=%s "
                    "age_hours=%.2f code=%s retryable=%s",
                    candidate.position_id,
                    candidate.provider,
                    age_hours,
                    exc.code,
                    exc.retryable,
                )
                continue
            except Exception:
                logger.exception(
                    "Stale position watchdog failed safely position=%s provider=%s age_hours=%.2f",
                    candidate.position_id,
                    candidate.provider,
                    age_hours,
                )
                continue

            closed_count += result.closed_count + result.already_closed_count
            logger.warning(
                "Stale position watchdog reconciled position=%s provider=%s "
                "age_hours=%.2f max_age_hours=%.2f closed=%s already_closed=%s failed=%s",
                candidate.position_id,
                candidate.provider,
                age_hours,
                candidate.max_age_hours,
                result.closed_count,
                result.already_closed_count,
                result.failed_count,
            )
        return closed_count

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Stale position watchdog cycle failed safely")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_seconds,
                )
            except TimeoutError:
                pass

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="super-signals-stale-position-watchdog",
        )
        logger.warning(
            "Stale position watchdog started default_max_age_hours=%.1f "
            "tig_max_age_hours=%.1f poll_seconds=%s",
            self._default_max_age_hours,
            self._tig_max_age_hours,
            self._poll_seconds,
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None


__all__ = ["StalePositionCandidate", "StalePositionWatchdog"]
