"""Keep AIDY's decision outcomes current inside the deployed application.

Same shape as ``aidy_decision_runtime.py``: no external credential needed, starts
whenever the application starts, writes only to ``aidy_decision_outcomes`` (append-only,
research_only, CHECK-constrained to never grant live_money_execution_allowed), and a
failed pass is logged and retried rather than allowed to end the loop.

Runs less often than the decision loop it follows: a decision needs its trade to have
actually resolved before there is anything to score, so there is no benefit to polling
as tightly as the 5-minute decision pass.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from app.aidy_decision_outcome_runner import AidyDecisionOutcomeRunner

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 900
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


class AidyDecisionOutcomeRuntime:
    """Run the outcome-scoring pass on a timer; never let it take the application down."""

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
            "AIDY_DECISION_OUTCOME_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._pass_limit = pass_limit or _positive_int(
            "AIDY_DECISION_OUTCOME_PASS_LIMIT", _DEFAULT_PASS_LIMIT
        )
        self._batch_size = batch_size or _positive_int(
            "AIDY_DECISION_OUTCOME_BATCH_SIZE", _DEFAULT_BATCH_SIZE
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        if os.getenv("AIDY_DECISION_OUTCOME_ENGINE_ENABLED", "1").strip() == "0":
            logger.info("AIDY decision outcome scoring disabled by configuration")
            return False

        self._stopping.clear()
        runner = AidyDecisionOutcomeRunner(self._session_factory)
        self._task = asyncio.create_task(
            self._run(runner), name="super-signals-aidy-decision-outcomes"
        )
        logger.info(
            "AIDY decision outcome scoring loop started interval=%ss", self._interval_seconds
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

    async def _run(self, runner: AidyDecisionOutcomeRunner) -> None:
        while not self._stopping.is_set():
            try:
                summary = await runner.run(limit=self._pass_limit, batch_size=self._batch_size)
                logger.info(
                    "AIDY outcome scoring pass selected=%s written=%s by_resolution=%s "
                    "total_delta_usd=%s",
                    summary.selected,
                    summary.written,
                    summary.by_resolution,
                    summary.total_delta_usd,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a research pass must never end the service
                logger.exception("AIDY outcome scoring pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


__all__ = ["AidyDecisionOutcomeRuntime"]
