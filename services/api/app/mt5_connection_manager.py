"""Background MetaAPI connection-state monitor for Day 22."""

from __future__ import annotations

import asyncio
import logging
import os

from app.mt5_connection_service import Mt5DemoConnectionService

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 3600
MINIMUM_REFRESH_SECONDS = 300

# Upper bound on the first reconciliation attempt performed by the background
# monitor. It must never delay FastAPI from opening its HTTP port.
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
    """Reconcile stored MetaAPI accounts after restart and at a bounded cadence."""

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
        """Start reconciliation without blocking the web application's HTTP startup.

        Broker availability is an external dependency. A slow MetaAPI reconciliation
        must never prevent /health, the dashboard, or static app assets from becoming
        reachable. The monitor performs the same bounded reconciliation immediately in
        its own task and then continues at the normal idle cadence.
        """
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

    async def _startup_reconcile(self) -> None:
        try:
            checked = await asyncio.wait_for(
                self._service.reconcile_all(),
                timeout=_STARTUP_RECONCILE_TIMEOUT_SECONDS,
            )
            logger.info(
                "MT5 background startup reconciliation checked %d account(s); idle interval=%ds",
                checked,
                self._refresh_seconds,
            )
        except TimeoutError:
            logger.error(
                "MT5 background startup reconciliation exceeded %ds; web remains available "
                "and reconciliation will retry on the idle interval",
                _STARTUP_RECONCILE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("MT5 background startup reconciliation failed")

    async def _run(self) -> None:
        try:
            # Reconcile immediately after the monitor is scheduled, but outside the
            # FastAPI startup critical path. Nothing is assumed on failure: account
            # state remains unreconciled until the next bounded pass.
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
                    await self._service.reconcile_all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("MT5 connection reconciliation failed")
        except asyncio.CancelledError:
            raise
