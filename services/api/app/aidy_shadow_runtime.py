"""Application-owned AIDY Provider Lab replay and context runtime.

This runtime is intentionally independent of broker credentials. M1 replay remains the
primary research loop; context and Day 21 enrichment are optional/fail-flat and may
never prevent the existing resolver from starting or block live signal routing.
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
from app.provider_day21_full_chain import ProviderDay21ChainResolver

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
        if market_client is None:
            message = (
                "AIDY Provider Lab research loop not started "
                f"url_configured={url_configured} token_configured={token_configured}"
            )
            print(message, flush=True)
            logger.warning(message)
            return False

        context_client = AidyContextClient.from_environment()
        context_resolver: ProviderContextAttachmentResolver | None = None
        if context_client is not None:
            context_resolver = ProviderContextAttachmentResolver(
                self._session_factory,
                context_client,
            )
        else:
            logger.warning(
                "AIDY Provider Lab context attachment disabled; M1 research remains active"
            )

        self._stopping.clear()
        market_resolver = AidyShadowResolver(self._session_factory, market_client)
        day21_resolver = ProviderDay21ChainResolver(self._session_factory)
        self._task = asyncio.create_task(
            self._run(market_resolver, context_resolver, day21_resolver),
            name="super-signals-provider-aidy-research",
        )
        # Keep the established startup log contract for operational monitors/tests.
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

    async def _run(
        self,
        market_resolver: AidyShadowResolver,
        context_resolver: ProviderContextAttachmentResolver | None,
        day21_resolver: ProviderDay21ChainResolver,
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

            attached, context_failures, terminal_misses = 0, 0, 0
            if context_resolver is not None:
                # Context attachment is deliberately isolated from M1 replay and from
                # all broker/member execution. Transient AIDY failures retry later;
                # terminal PIT-stale outcomes are persisted once.
                try:
                    attached, context_failures = await context_resolver.resolve_once()
                    terminal_misses = context_resolver.last_terminal_misses
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "AIDY Provider Lab context attachment loop failed safely"
                    )
                    attached, context_failures, terminal_misses = 0, 1, 0

                context_message = (
                    "AIDY Provider Lab context attachment "
                    f"attached={attached} terminal_misses={terminal_misses} "
                    f"failures={context_failures}"
                )
                print(context_message, flush=True)
                if attached or terminal_misses or context_failures:
                    logger.info(context_message)

            day21_processed, day21_failures = 0, 0
            try:
                # Day 21 is a DB-only append-only research observer. Running it after
                # context attachment means newly attached PIT context is visible in the
                # same pass. It contains no broker call and cannot block routing.
                day21_processed, day21_failures = day21_resolver.resolve_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Provider Day21 full-chain loop failed safely")
                day21_processed, day21_failures = 0, 1

            day21_message = (
                "Provider Day21 full-chain "
                f"processed={day21_processed} failures={day21_failures}"
            )
            print(day21_message, flush=True)
            if day21_processed or day21_failures:
                logger.info(day21_message)

            if draining_startup_backlog:
                startup_pass += 1
                if (
                    processed > 0
                    or attached > 0
                    or terminal_misses > 0
                    or day21_processed > 0
                ) and startup_pass < self._startup_pass_limit:
                    await asyncio.sleep(0)
                    continue
                drain_message = (
                    "AIDY Provider Lab startup research drain complete "
                    f"passes={startup_pass} last_m1_processed={processed} "
                    f"last_context_attached={attached} "
                    f"last_context_terminal_misses={terminal_misses} "
                    f"last_day21_processed={day21_processed} "
                    f"m1_failures={market_failures} context_failures={context_failures} "
                    f"day21_failures={day21_failures}"
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
