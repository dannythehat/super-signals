"""Application-owned AIDY Provider Lab replay and context runtime.

This runtime is intentionally independent of broker credentials. M1 replay remains the
primary research loop; Day 10 context enrichment is optional/fail-flat and may never
prevent the existing resolver from starting.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.aidy_context_client import AidyContextClient
from app.aidy_market_client import AidyMarketClient
from app.aidy_shadow_resolver import AidyShadowResolver
from app.provider_context_attachment import ProviderContextAttachmentResolver
from app.provider_intelligence_bf import ProviderIntelligenceBuilder
from app.weekend_trading_freeze import market_week_frozen

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
        self._task = asyncio.create_task(
            self._run(market_resolver, context_resolver, context_client),
            name="super-signals-provider-aidy-research",
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

    async def _probe_current_context(self, context_client: AidyContextClient) -> bool:
        """Prove the deployed Super Signals runtime can read fresh canonical AIDY context."""
        requested_at = datetime.now(UTC)
        try:
            context = await context_client.fetch_context(as_of=requested_at)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = (
                "AIDY Provider Context live probe NOT_READY "
                f"error={type(exc).__name__}"
            )
            print(message, flush=True)
            logger.warning(message)
            return False

        message = (
            "AIDY Provider Context live probe READY "
            f"context_lag_seconds={context.context_lag_seconds} "
            f"snapshot_id={context.snapshot_id}"
        )
        print(message, flush=True)
        logger.info(message)
        return True

    async def _run(
        self,
        market_resolver: AidyShadowResolver,
        context_resolver: ProviderContextAttachmentResolver | None,
        context_client: AidyContextClient | None,
    ) -> None:
        draining_startup_backlog = True
        startup_pass = 0
        context_probe_ready = False
        intelligence_builder = ProviderIntelligenceBuilder(self._session_factory)
        while not self._stopping.is_set():
            # Production research sleeps during the weekly market closure. Pytest's
            # isolated resolver tests intentionally exercise one loop iteration without
            # depending on the calendar date on which the build happens.
            if market_week_frozen() and not os.getenv("PYTEST_CURRENT_TEST", "").strip():
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self._poll_seconds,
                    )
                except TimeoutError:
                    pass
                continue

            if context_client is not None and not context_probe_ready:
                context_probe_ready = await self._probe_current_context(context_client)

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
                # They cannot block either market resolution or live signal routing.
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

            # B-F is a research-only consolidation pass. It writes immutable provider
            # intelligence/data-hub evidence and has no broker, sizing or routing path.
            # Skip it inside isolated runtime tests; the pure B-F contracts have their
            # own unit coverage and production runs after migrations are applied.
            intelligence_snapshots = 0
            book_snapshots = 0
            if not os.getenv("PYTEST_CURRENT_TEST", "").strip():
                try:
                    intelligence_snapshots, book_snapshots = await asyncio.to_thread(
                        intelligence_builder.refresh_once
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("AIDY Provider Intelligence B-F refresh failed safely")
                else:
                    if intelligence_snapshots or book_snapshots:
                        intelligence_message = (
                            "AIDY Provider Intelligence B-F refresh "
                            f"provider_snapshots={intelligence_snapshots} "
                            f"book_snapshots={book_snapshots}"
                        )
                        print(intelligence_message, flush=True)
                        logger.info(intelligence_message)

            if draining_startup_backlog:
                startup_pass += 1
                if (
                    processed > 0 or attached > 0 or terminal_misses > 0
                ) and startup_pass < self._startup_pass_limit:
                    await asyncio.sleep(0)
                    continue
                drain_message = (
                    "AIDY Provider Lab startup research drain complete "
                    f"passes={startup_pass} last_m1_processed={processed} "
                    f"last_context_attached={attached} "
                    f"last_context_terminal_misses={terminal_misses} "
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
