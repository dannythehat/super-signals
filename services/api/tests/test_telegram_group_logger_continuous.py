from __future__ import annotations

import asyncio
from uuid import uuid4

from app.telegram_publisher import PublicationAttempt
from app.telegram_publisher_day34_cutover import Day34CutoverTelegramPublisherManager


class _ContinuousPublisherHarness(Day34CutoverTelegramPublisherManager):
    def __init__(self) -> None:
        # Exercise only the runtime loop. No DB or Telegram connection is needed.
        self._stop_event = asyncio.Event()
        self._poll_seconds = 0.001
        self._reader_exclusion_active = True
        self.seed_count = 0
        self.claim_count = 0
        self.delivered: list[PublicationAttempt] = []

    def _active_reader_source_collision(self) -> bool:
        return False

    def check_connection(self):
        raise AssertionError("per-cycle auxiliary connection checks must not gate delivery")

    def _seed_missing_publications(self) -> None:
        self.seed_count += 1

    def _claim_next(self):
        self.claim_count += 1
        if self.claim_count <= 2:
            return PublicationAttempt(
                publication_id=uuid4(),
                signal_id=uuid4(),
                text=f"trade-{self.claim_count}",
            )
        return None

    async def _deliver(self, attempt: PublicationAttempt) -> None:
        self.delivered.append(attempt)
        if len(self.delivered) == 2:
            self._stop_event.set()


def test_group_logger_publishes_second_trade_without_restart() -> None:
    publisher = _ContinuousPublisherHarness()

    asyncio.run(publisher._run())

    assert publisher.seed_count >= 2
    assert publisher.claim_count >= 2
    assert [item.text for item in publisher.delivered] == ["trade-1", "trade-2"]


class _CycleRecoveryHarness(_ContinuousPublisherHarness):
    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    def _seed_missing_publications(self) -> None:
        self.seed_count += 1
        if not self.failed_once:
            self.failed_once = True
            raise RuntimeError("simulated logger-only cycle failure")

    def _claim_next(self):
        self.claim_count += 1
        return PublicationAttempt(
            publication_id=uuid4(),
            signal_id=uuid4(),
            text="recovered",
        )

    async def _deliver(self, attempt: PublicationAttempt) -> None:
        self.delivered.append(attempt)
        self._stop_event.set()


def test_group_logger_recovers_after_one_cycle_failure() -> None:
    publisher = _CycleRecoveryHarness()

    asyncio.run(publisher._run())

    assert publisher.seed_count >= 2
    assert publisher.claim_count == 1
    assert [item.text for item in publisher.delivered] == ["recovered"]
