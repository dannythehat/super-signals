"""Application-owned AIDY Provider Lab replay and context runtime.

This runtime is intentionally independent of broker credentials. It needs only the
Super Signals database session factory and authenticated read-only AIDY clients.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from app.aidy_context_client import AidyContextClient
from app.aidy_market_client import AidyMarketClient
from app.aidy_shadow_resolver import AidyShadowResolver
from app.provider_context_attachment import ProviderContextAttachmentResolver

logger = logging.getLogger(__name__)

_DEFAULT_POLL_SECONDS = 300
_DEFAULT_STARTUP_PASS_LIMIT = 96


class AidyShadowRuntime:
    """Own AIDY research loops at application scope, never broker scope."""

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
        market_client = AidyMarketClient.from_environment()
        context_client = AidyContextClient.from_environment()
        if market_client is None or context_client is None:
            message = (
                "AIDY Provider Lab research loop not started "
                f"url_configured={url_configured} token_configured={token_configured}"
            )
            print(message, flush=True)
            logger.warning(message)
            return False
        self._stopping.clear()
        market_resolver = AidyShadowResolver(self._session_factory, market_client)
        context_resolver = ProviderContextAttachmentResolver(self._session_factory, context_client)
        self._task = asyncio.create_task(
            self._run(market_resolver, context_resolver),
            name="super-signals-provider-aidy-research",
        )
        print("AIDY Provider Lab research loop started", flush=True)
        logger.info("AIDY Provider Lab research loop started")
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

    async def _run(
        self,
        market_resolver: AidyShadowResolver,
        context_resolver: ProviderContextAttachmentResolver,
    ) -> None:
        draining_startup_backlog = True
        startup_pass = 0
        while not self._stopping.is_set():
            try:
                processed, market_failures = await market_resolver.resolve_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AIDY Provider Lab M1 resolver loop failed safely")
                processed, market_failures = 0, 1

            market_message = (
                "AIDY Provider Lab M1 resolution "
                f"processed={processed} failures={market_failures}"
            )
            print(market_message, flush=True)
            if processed or market_failures:
                logger.info(market_message)

            # Context attachment is deliberately isolated from M1 replay and from all
            # broker/member execution. A failed AIDY context request retries later and
            # cannot block either market resolution or live signal routing.
            try:
                attached, context_failures = await context_resolver.resolve_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AIDY Provider Lab context attachment loop failed safely")
                attached, context_failures = 0, 1

            context_message = (
                "AIDY Provider Lab context attachment "
                f"attached={attached} failures={context_failures}"
            )
            print(context_message, flush=True)
            if attached or context_failures:
                logger.info(context_message)

            if draining_startup_backlog:
                startup_pass += 1
                if (processed > 0 or attached > 0) and startup_pass < self._startup_pass_limit:
                    await asyncio.sleep(0)
                    continue
                drain_message = (
                    "AIDY Provider Lab startup research drain complete "
                    f"passes={startup_pass} last_m1_processed={processed} "
                    f"last_context_attached={attached} "
                    f"m1_failures={market_failures} context_failures={context_failures}"
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
