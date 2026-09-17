"""Keep AIDY's decision ledger current inside the deployed application.

Unlike the M1 resolver or the trade scorer, this loop needs no external credential --
duplicate/conflict detection and track-record evaluation read only this application's
own database, so there is no "not configured" case to fail flat on. It starts whenever
the application starts.

The loop is deliberately as quiet and safe as the scoring loop it sits beside: it holds
no broker credential, writes only to ``aidy_decisions`` (append-only, research_only,
CHECK-constrained to never grant live_money_execution_allowed), and a failed pass is
logged and retried rather than allowed to end the loop or touch anything else. Nothing
in Super Signals' execution path reads this table -- it is entirely downstream of
decisions already made and money already at risk.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from app.aidy_decision_runner import AidyDecisionRunner

logger = logging.getLogger(__name__)

# Runs more often than the 30-minute scoring loop it complements: a decision is meant
# to exist close to when the signal was posted, not half an hour later.
_DEFAULT_INTERVAL_SECONDS = 300
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


class AidyDecisionRuntime:
    """Run the decision pass on a timer; never let it take the application down."""

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
            "AIDY_DECISION_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._pass_limit = pass_limit or _positive_int(
            "AIDY_DECISION_PASS_LIMIT", _DEFAULT_PASS_LIMIT
        )
        self._batch_size = batch_size or _positive_int(
            "AIDY_DECISION_BATCH_SIZE", _DEFAULT_BATCH_SIZE
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        if os.getenv("AIDY_DECISION_ENGINE_ENABLED", "1").strip() == "0":
            logger.info("AIDY decision engine disabled by configuration")
            return False

        self._stopping.clear()
        runner = AidyDecisionRunner(self._session_factory)
        self._task = asyncio.create_task(self._run(runner), name="super-signals-aidy-decisions")
        logger.info("AIDY decision engine loop started interval=%ss", self._interval_seconds)
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

    async def _run(self, runner: AidyDecisionRunner) -> None:
        while not self._stopping.is_set():
            try:
                summary = await runner.run(limit=self._pass_limit, batch_size=self._batch_size)
                logger.info(
                    "AIDY decision pass selected=%s written=%s by_class=%s",
                    summary.selected,
                    summary.written,
                    summary.by_class,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a research pass must never end the service
                logger.exception("AIDY decision pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


__all__ = ["AidyDecisionRuntime"]
