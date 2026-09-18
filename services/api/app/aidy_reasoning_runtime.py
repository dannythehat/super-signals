"""Run AIDY's reasoning pass on a timer -- off by default, exactly like the module it extends.

aidy_decision_engine.py's own docstring is explicit: a real model call per signal "is a
real ongoing cost, and it is not switched on silently." This loop is built, tested and
deployable, but `AIDY_REASONING_ENGINE_ENABLED` defaults to "0" -- it ships disabled so
turning on real spend is one env var the owner sets deliberately, not a side effect of
this PR merging. A missing API key or budget exhaustion never crashes the loop; both are
logged and the pass is simply skipped until the next tick.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from app.aidy_context_client import AidyContextClient
from app.aidy_market_client import AidyMarketClient
from app.aidy_reasoning_engine import AidyReasoningEngine
from app.aidy_reasoning_runner import AidyReasoningRunner

logger = logging.getLogger(__name__)

# Matches the 300s decision loop -- with v2's scope covering every approve decision
# (2,598 in the historical backlog at build time), the budget gate is what actually
# bounds spend, not a deliberately slow interval; a real backlog deserves draining in
# well under an hour, not overnight.
_DEFAULT_INTERVAL_SECONDS = 300
_DEFAULT_PASS_LIMIT = 300


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


def _api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("Open", "").strip()


class AidyReasoningRuntime:
    """Run the reasoning pass on a timer; never let it take the application down."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interval_seconds: int | None = None,
        pass_limit: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds or _positive_int(
            "AIDY_REASONING_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._pass_limit = pass_limit or _positive_int(
            "AIDY_REASONING_PASS_LIMIT", _DEFAULT_PASS_LIMIT
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        if os.getenv("AIDY_REASONING_ENGINE_ENABLED", "0").strip() != "1":
            logger.info("AIDY reasoning engine disabled by configuration")
            return False
        api_key = _api_key()
        if not api_key:
            logger.warning("AIDY reasoning engine enabled but no OpenAI API key is configured")
            return False

        self._stopping.clear()
        runner = AidyReasoningRunner(
            self._session_factory,
            engine=AidyReasoningEngine(
                api_key=api_key,
                model=os.getenv("AIDY_REASONING_MODEL", "gpt-5-mini-2025-08-07").strip(),
            ),
            context_client=AidyContextClient.from_environment(),
            candle_client=AidyMarketClient.from_environment(),
        )
        self._task = asyncio.create_task(self._run(runner), name="super-signals-aidy-reasoning")
        logger.info("AIDY reasoning engine loop started interval=%ss", self._interval_seconds)
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

    async def _run(self, runner: AidyReasoningRunner) -> None:
        while not self._stopping.is_set():
            try:
                summary = await runner.run(limit=self._pass_limit)
                logger.info(
                    "AIDY reasoning pass selected=%s written=%s failed=%s skipped_budget=%s",
                    summary.selected,
                    summary.written,
                    summary.failed,
                    summary.skipped_budget,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a research pass must never end the service
                logger.exception("AIDY reasoning pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


__all__ = ["AidyReasoningRuntime"]
