"""Research must never be able to take the trading service down.

The scoring loop runs inside the process that mirrors real money to a broker. It holds
no broker credential and writes only to its own research table, but the part that needs
pinning is failure: a pass that throws has to leave the previous figures standing and
the service serving, not blank the scoreboard or end the application.
"""

from __future__ import annotations

import asyncio

import pytest

from app.provider_trade_scoring_runtime import ProviderTradeScoringRuntime


class StubRunner:
    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self._raises = raises
        self.seen = asyncio.Event()

    async def run(self, *, limit: int, batch_size: int):
        self.calls += 1
        self.seen.set()
        if self._raises:
            raise RuntimeError("aidy unreachable")

        class _Summary:
            selected = 0
            written = 0
            outcomes: dict[str, int] = {}

        return _Summary()


def test_scoring_does_not_start_without_aidy_credentials(monkeypatch) -> None:
    """The same absence that stops the M1 replay loop; live traffic is unaffected."""
    monkeypatch.delenv("AIDY_PROVIDER_MARKET_URL", raising=False)
    monkeypatch.delenv("AIDY_PROVIDER_MARKET_TOKEN", raising=False)
    runtime = ProviderTradeScoringRuntime(None)

    assert asyncio.run(runtime.start()) is False
    assert runtime.running is False


def test_scoring_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setenv("PROVIDER_SCORING_ENABLED", "0")
    runtime = ProviderTradeScoringRuntime(None)

    assert asyncio.run(runtime.start()) is False


def test_a_failing_pass_does_not_end_the_loop() -> None:
    async def scenario() -> int:
        runtime = ProviderTradeScoringRuntime(None, interval_seconds=1)
        runner = StubRunner(raises=True)
        runtime._task = asyncio.create_task(runtime._run(runner))
        await asyncio.wait_for(runner.seen.wait(), timeout=5)
        await asyncio.sleep(1.2)
        still_running = runtime.running
        await runtime.stop()
        assert still_running, "a failed pass must not end the loop"
        return runner.calls

    assert asyncio.run(scenario()) >= 2


def test_stopping_is_prompt_rather_than_waiting_out_the_interval() -> None:
    """Shutdown cannot be held up for half an hour by a sleeping research loop."""

    async def scenario() -> None:
        runtime = ProviderTradeScoringRuntime(None, interval_seconds=3600)
        runner = StubRunner()
        runtime._task = asyncio.create_task(runtime._run(runner))
        await asyncio.wait_for(runner.seen.wait(), timeout=5)
        await asyncio.wait_for(runtime.stop(), timeout=5)

    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["0", "-5", "not-a-number"])
def test_a_nonsense_interval_falls_back_to_the_default(monkeypatch, value) -> None:
    monkeypatch.setenv("PROVIDER_SCORING_INTERVAL_SECONDS", value)

    assert ProviderTradeScoringRuntime(None)._interval_seconds == 1800
