"""Keep the provider scoreboard current inside the deployed application.

Scoring needs the database and AIDY's research path, both of which the running service
already holds, so it belongs here rather than in a hand-run script against production
credentials.

The loop exists because a score is derived rather than observed. A trade on a day AIDY
never captured scores "no price history" now and gets a real answer the moment those
minutes are backfilled, so the pass has to be repeatable and has to re-ask exactly the
rows whose answer can still change. It is also why this is slow and quiet: it holds no
broker credential, writes only to ``provider_trade_scores``, and a failure leaves the
previous figures standing rather than blanking the scoreboard.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from app.aidy_market_client import AidyMarketClient
from app.provider_trade_scoring_runner import ProviderTradeScoringRunner

logger = logging.getLogger(__name__)

# Backfills arrive in batches rather than continuously, so re-asking every half hour is
# frequent enough to pick them up and rare enough to stay out of the way of the M1
# replay loop, which is the research path that actually gates promotion.
_DEFAULT_INTERVAL_SECONDS = 1800
# Bounds one pass. A first run faces roughly 2,300 trades; batching keeps its results
# landing steadily instead of in one long transaction at the end.
_DEFAULT_PASS_LIMIT = 2500
_DEFAULT_BATCH_SIZE = 100


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer; using %s", name, default)
        return default
    if value <= 0:
        logger.warning("%s must be positive; using %s", name, default)
        return default
    return value


class ProviderTradeScoringRuntime:
    """Run the scoring pass on a timer, and never let it take the application down."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interval_seconds: int | None = None,
        pass_limit: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds or _positive_int(
            "PROVIDER_SCORING_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._pass_limit = pass_limit or _positive_int(
            "PROVIDER_SCORING_PASS_LIMIT", _DEFAULT_PASS_LIMIT
        )
        self._batch_size = batch_size or _positive_int(
            "PROVIDER_SCORING_BATCH_SIZE", _DEFAULT_BATCH_SIZE
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        if os.getenv("PROVIDER_SCORING_ENABLED", "1").strip() == "0":
            logger.info("Provider trade scoring disabled by configuration")
            return False
        client = AidyMarketClient.from_environment()
        if client is None:
            # The same absence that stops the M1 replay loop. Research is optional; the
            # application serves live traffic without it.
            logger.warning(
                "Provider trade scoring not started; AIDY research credentials absent"
            )
            return False

        self._stopping.clear()
        runner = ProviderTradeScoringRunner(self._session_factory, client)
        self._task = asyncio.create_task(
            self._run(runner), name="super-signals-provider-trade-scoring"
        )
        logger.info(
            "Provider trade scoring loop started interval=%ss", self._interval_seconds
        )
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

    async def _run(self, runner: ProviderTradeScoringRunner) -> None:
        while not self._stopping.is_set():
            try:
                summary = await runner.run(
                    limit=self._pass_limit, batch_size=self._batch_size
                )
                logger.info(
                    "Provider trade scoring pass selected=%s written=%s outcomes=%s",
                    summary.selected,
                    summary.written,
                    summary.outcomes,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a research pass must never end the service
                logger.exception("Provider trade scoring pass failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue


__all__ = ["ProviderTradeScoringRuntime"]
