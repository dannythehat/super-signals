"""Application-owned AIDY Provider Lab replay runtime.

This runtime is intentionally independent of broker credentials. It needs only the
Super Signals database session factory and the authenticated AIDY market client.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from app.aidy_market_client import AidyMarketClient
from app.aidy_shadow_resolver import AidyShadowResolver

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SECONDS = 300
_DEFAULT_STARTUP_PASS_LIMIT = 96


class AidyShadowRuntime:
    """Own the AIDY resolver loop at application scope, not broker scope."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        poll_seconds: int = _DEFAULT_POLL_SECONDS,
        startup_pass_limit: int = _DEFAULT_STARTUP_PASS_LIMIT,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("aidy_shadow_poll_seconds_must_be_positive")
        if startup_pass_limit <= 0:
            raise ValueError("aidy_shadow_startup_pass_limit_must_be_positive")
        self._session_factory = session_factory
        self._poll_seconds = poll_seconds
        self._startup_pass_limit = startup_pass_limit
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        url_configured = bool(os.getenv("AIDY_PROVIDER_MARKET_URL", "").strip())
        token_configured = bool(os.getenv("AIDY_PROVIDER_MARKET_TOKEN", "").strip())
        client = AidyMarketClient.from_environment()
        if client is None:
            message = (
                "AIDY Provider Lab resolver loop not started "
                f"url_configured={url_configured} token_configured={token_configured}"
            )
            print(message, flush=True)
            logger.warning(message)
            return False
        self._stopping.clear()
        resolver = AidyShadowResolver(self._session_factory, client)
        self._task = asyncio.create_task(
            self._run(resolver),
            name="super-signals-shadow-aidy-m1",
        )
        print("AIDY Provider Lab resolver loop started", flush=True)
        logger.info("AIDY Provider Lab resolver loop started")
        return True

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stopping.set()
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self, resolver: AidyShadowResolver) -> None:
        draining_startup_backlog = True
        startup_pass = 0
        while not self._stopping.is_set():
            try:
                processed, failures = await resolver.resolve_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AIDY Provider Lab resolver loop failed safely")
                processed, failures = 0, 1

            batch_message = (
                "AIDY Provider Lab M1 resolution "
                f"processed={processed} failures={failures}"
            )
            print(batch_message, flush=True)
            if processed or failures:
                logger.info(batch_message)

            if draining_startup_backlog:
                startup_pass += 1
                if processed > 0 and startup_pass < self._startup_pass_limit:
                    await asyncio.sleep(0)
                    continue
                drain_message = (
                    "AIDY Provider Lab startup backfill drain complete "
                    f"passes={startup_pass} last_processed={processed} failures={failures}"
                )
                print(drain_message, flush=True)
                logger.info(drain_message)
                draining_startup_backlog = False

            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._poll_seconds,
                )
            except TimeoutError:
                pass


__all__ = ["AidyShadowRuntime"]
