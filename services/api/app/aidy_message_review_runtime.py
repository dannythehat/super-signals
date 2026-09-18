"""Run AIDY's research-only review of recent skipped provider messages."""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from app.aidy_message_review_engine import AidyMessageReviewEngine
from app.aidy_message_review_runner import AidyMessageReviewRunner

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 120
_DEFAULT_PASS_LIMIT = 40


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer; using %s", name, default)
        return default
    return value if value > 0 else default


def _api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("Open", "").strip()


class AidyMessageReviewRuntime:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interval_seconds: int | None = None,
        pass_limit: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds or _positive_int(
            "AIDY_MESSAGE_REVIEW_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._pass_limit = pass_limit or _positive_int(
            "AIDY_MESSAGE_REVIEW_PASS_LIMIT", _DEFAULT_PASS_LIMIT
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        if os.getenv("AIDY_MESSAGE_REVIEW_ENABLED", "1").strip() == "0":
            logger.info("AIDY message review disabled by configuration")
            return False
        api_key = _api_key()
        if not api_key:
            logger.warning("AIDY message review not started; OpenAI key missing")
            return False

        runner = AidyMessageReviewRunner(
            self._session_factory,
            engine=AidyMessageReviewEngine(
                api_key=api_key,
                model=os.getenv("AIDY_REASONING_MODEL", "gpt-5-mini-2025-08-07").strip(),
            ),
        )
        self._stopping.clear()
        self._task = asyncio.create_task(
            self._run(runner), name="super-signals-aidy-message-review"
        )
        logger.info(
            "AIDY message review loop started interval=%ss", self._interval_seconds
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

    async def _run(self, runner: AidyMessageReviewRunner) -> None:
        while not self._stopping.is_set():
            try:
                summary = await runner.run(limit=self._pass_limit)
                logger.info(
                    "AIDY message review pass selected=%s written=%s failed=%s classes=%s budget=%s",
                    summary.selected,
                    summary.written,
                    summary.failed,
                    summary.by_class,
                    summary.skipped_budget,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AIDY message review pass failed safely")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                continue


__all__ = ["AidyMessageReviewRuntime"]
