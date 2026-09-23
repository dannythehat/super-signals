"""Isolated research runtime for provider management-language coverage."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session, sessionmaker

from app.provider_management_language_audit import ProviderManagementLanguageAuditService

logger = logging.getLogger(__name__)


class ProviderManagementLanguageAuditRuntime:
    """Refresh provider dialect coverage without touching live trading."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interval_seconds: int = 21600,
    ) -> None:
        self._service = ProviderManagementLanguageAuditService(session_factory)
        self._interval_seconds = max(900, int(interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(
                self._run(),
                name="provider-management-language-audit",
            )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(2)
        while not self._stopping.is_set():
            try:
                count = await asyncio.to_thread(self._service.refresh_all)
                logger.info(
                    "Provider management-language audit refreshed %d sources on research lane",
                    count,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Provider management-language audit failed safely; live trading unchanged"
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                continue


__all__ = ["ProviderManagementLanguageAuditRuntime"]
