"""Run the provider fingerprint pass daily -- the underlying data does not change fast enough
to justify anything more frequent, and the owner explicitly asked for a daily cadence.

Enabled by default: unlike the reasoning engine, this makes no external API call and costs
nothing beyond a handful of read-only queries already used elsewhere, so there is no spend
decision to gate behind an env var.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from app.provider_fingerprint_runner import ProviderFingerprintRunner

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 86400


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


class ProviderFingerprintRuntime:
    """Run the fingerprint pass on a timer; never let it take the application down."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interval_seconds: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds or _positive_int(
            "PROVIDER_FINGERPRINT_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        if os.getenv("PROVIDER_FINGERPRINT_ENABLED", "1").strip() == "0":
            logger.info("Provider fingerprint engine disabled by configuration")
            return False

        self._stopping.clear()
        runner = ProviderFingerprintRunner(self._session_factory)
        self._task = asyncio.create_task(
            self._run(runner), name="super-signals-provider-fingerprint"
        )
        logger.info("Provider fingerprint loop started interval=%ss", self._interval_seconds)
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

    async def _run(self, runner: ProviderFingerprintRunner) -> None:
        # Fire immediately on startup rather than waiting a full day for the first pass.
        while not self._stopping.is_set():
            try:
                summary = await runner.run()
                logger.info("Provider fingerprint pass computed=%s", summary.computed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a research pass must never end the service
                logger.exception("Provider fingerprint pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


__all__ = ["ProviderFingerprintRuntime"]
