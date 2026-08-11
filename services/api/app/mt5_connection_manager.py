"""Background MetaAPI connection-state monitor for Day 22."""

from __future__ import annotations

import asyncio
import logging

from app.mt5_connection_service import Mt5DemoConnectionService

logger = logging.getLogger(__name__)


class Mt5ConnectionManager:
    """Reconcile stored MetaAPI accounts after restart and at a bounded cadence."""

    def __init__(
        self,
        service: Mt5DemoConnectionService,
        *,
        refresh_seconds: int = 300,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("MT5 connection refresh interval must be positive.")
        self._service = service
        self._refresh_seconds = refresh_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        # Reconcile once before the loop. This is the Day 22 restart/reconnect path.
        # Day 22 has no trade engine, so idle polling stays deliberately slow to
        # avoid unnecessary MetaAPI traffic and development spend.
        try:
            checked = await self._service.reconcile_all()
            logger.info("MT5 connection startup reconciliation checked %d account(s)", checked)
        except Exception:
            logger.exception("MT5 connection startup reconciliation failed")
        self._task = asyncio.create_task(self._run(), name="super-signals-mt5-connection-monitor")

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

    async def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._refresh_seconds
                    )
                    continue
                except TimeoutError:
                    pass
                try:
                    await self._service.reconcile_all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("MT5 connection reconciliation failed")
        except asyncio.CancelledError:
            raise
