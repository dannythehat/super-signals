"""Background MetaAPI connection-state monitor for Day 22+ MT5 accounts."""

from __future__ import annotations

import asyncio
import logging
import os

from app.mt5_connection_service import Mt5DemoConnectionService

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 3600
MINIMUM_REFRESH_SECONDS = 300
_STARTUP_RECONCILE_TIMEOUT_SECONDS = 30.0


def _configured_refresh_seconds() -> int:
    raw = os.getenv("SUPER_SIGNALS_MT5_RECONCILE_SECONDS", "").strip()
    if not raw:
        return DEFAULT_REFRESH_SECONDS
    try:
        seconds = int(raw)
    except ValueError:
        logger.warning(
            "Invalid SUPER_SIGNALS_MT5_RECONCILE_SECONDS; using %d seconds",
            DEFAULT_REFRESH_SECONDS,
        )
        return DEFAULT_REFRESH_SECONDS
    if seconds < MINIMUM_REFRESH_SECONDS:
        logger.warning(
            "SUPER_SIGNALS_MT5_RECONCILE_SECONDS below safe minimum; using %d seconds",
            MINIMUM_REFRESH_SECONDS,
        )
        return MINIMUM_REFRESH_SECONDS
    return seconds


class Mt5ConnectionManager:
    """Reconcile stored MetaAPI accounts without ever freezing the web service.

    The production Day22/Day30 reconciliation code contains synchronous SQLAlchemy
    work around asynchronous MetaAPI calls. Running that coroutine directly on
    Uvicorn's event loop can therefore stall /health and every browser request while
    PostgreSQL or MetaAPI is slow. Production reconciliation is executed in a worker
    thread with its own event loop. Test doubles and the original base service keep
    the normal async path, preserving the small unit-test contract.
    """

    def __init__(
        self,
        service: Mt5DemoConnectionService,
        *,
        refresh_seconds: int | None = None,
    ) -> None:
        resolved_refresh_seconds = (
            _configured_refresh_seconds() if refresh_seconds is None else refresh_seconds
        )
        if resolved_refresh_seconds <= 0:
            raise ValueError("MT5 connection refresh interval must be positive.")
        self._service = service
        self._refresh_seconds = resolved_refresh_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(
            self._run(),
            name="super-signals-mt5-connection-monitor",
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    @staticmethod
    def _run_coroutine_in_worker(service: Mt5DemoConnectionService) -> int:
        """Run one production reconciliation in an isolated event-loop thread."""
        return asyncio.run(service.reconcile_all())

    async def _reconcile_once(self) -> int:
        # Import lazily so this monitor stays independent of the Day22/Day30 modules
        # during module initialization. Day30 subclasses Day22, so both production
        # services take the isolated worker path.
        from app.mt5_connection_service_day22 import Day22Mt5DemoConnectionService

        if isinstance(self._service, Day22Mt5DemoConnectionService):
            return await asyncio.to_thread(self._run_coroutine_in_worker, self._service)
        return await self._service.reconcile_all()

    async def _startup_reconcile(self) -> None:
        try:
            checked = await asyncio.wait_for(
                self._reconcile_once(),
                timeout=_STARTUP_RECONCILE_TIMEOUT_SECONDS,
            )
            logger.info(
                "MT5 background startup reconciliation checked %d account(s); idle interval=%ds",
                checked,
                self._refresh_seconds,
            )
        except TimeoutError:
            logger.error(
                "MT5 background startup reconciliation exceeded %ds; isolated worker may finish "
                "later but web traffic remains independent",
                _STARTUP_RECONCILE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MT5 background startup reconciliation failed")

    async def _run(self) -> None:
        try:
            await self._startup_reconcile()

            while not self._stopping.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._refresh_seconds
                    )
                    continue
                except TimeoutError:
                    pass
                try:
                    await self._reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("MT5 connection reconciliation failed")
        except asyncio.CancelledError:
            raise
