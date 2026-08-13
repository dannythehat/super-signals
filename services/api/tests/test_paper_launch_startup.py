import asyncio

import pytest

from app.telegram_publisher_policy import Day19TelegramPublisherManager


@pytest.mark.asyncio
async def test_publisher_start_does_not_wait_for_telegram_verification(monkeypatch) -> None:
    verification_started = asyncio.Event()
    release_verification = asyncio.Event()

    async def blocked_verification(self) -> None:
        verification_started.set()
        await release_verification.wait()

    monkeypatch.setattr(
        Day19TelegramPublisherManager,
        "_record_startup_status",
        blocked_verification,
    )
    manager = Day19TelegramPublisherManager(
        session_factory=lambda: None,
        enabled=False,
        bot_token=None,
        destination_chat_id=None,
    )

    await asyncio.wait_for(manager.start(), timeout=0.1)
    await asyncio.wait_for(verification_started.wait(), timeout=0.1)
    assert manager._startup_status_task is not None
    assert not manager._startup_status_task.done()

    release_verification.set()
    await manager.stop()
