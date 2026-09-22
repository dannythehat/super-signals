from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.aidy_shadow_runtime as runtime_module
from app.aidy_shadow_runtime import AidyShadowRuntime

ROOT = Path(__file__).resolve().parents[1]


class _FakeResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_once(self) -> tuple[int, int]:
        self.calls += 1
        return 0, 0


class _SlowResolver:
    async def resolve_once(self) -> tuple[int, int]:
        await asyncio.sleep(0.2)
        return 1, 0


class _FakeContextClient:
    async def fetch_context(self, *, as_of):
        return SimpleNamespace(
            context_lag_seconds=42,
            snapshot_id="provider-context-snapshot-test",
        )


@pytest.mark.asyncio
async def test_aidy_runtime_starts_without_broker_credentials(monkeypatch, caplog) -> None:
    monkeypatch.delenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS", raising=False)
    monkeypatch.delenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS", raising=False)
    fake_resolver = _FakeResolver()
    sentinel_client = object()
    monkeypatch.setattr(
        runtime_module.AidyMarketClient,
        "from_environment",
        lambda: sentinel_client,
    )
    monkeypatch.setattr(
        runtime_module,
        "AidyShadowResolver",
        lambda session_factory, client: fake_resolver,
    )

    runtime = AidyShadowRuntime(
        object(),
        poll_seconds=60,
        startup_pass_limit=1,
    )
    with caplog.at_level(logging.INFO):
        assert await runtime.start() is True
        await asyncio.sleep(0)
        assert runtime.running is True
        assert fake_resolver.calls >= 1
        assert "AIDY Provider Lab resolver loop started" in caplog.text
    await runtime.stop()
    assert runtime.running is False


@pytest.mark.asyncio
async def test_aidy_runtime_live_context_probe_reports_ready(caplog) -> None:
    runtime = AidyShadowRuntime(
        object(),
        poll_seconds=60,
        startup_pass_limit=1,
    )
    with caplog.at_level(logging.INFO):
        assert await runtime._probe_current_context(_FakeContextClient()) is True
    assert "AIDY Provider Context live probe READY" in caplog.text
    assert "context_lag_seconds=42" in caplog.text
    assert "snapshot_id=provider-context-snapshot-test" in caplog.text


def test_main_lifespan_uses_separate_aidy_database_factory() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    live_factory = "session_factory = get_session_factory()"
    research_factory = "aidy_session_factory = get_aidy_session_factory()"
    construct = "aidy_shadow_runtime = AidyShadowRuntime(aidy_session_factory)"
    broker_gate = "if broker_keys:"
    assert live_factory in source
    assert research_factory in source
    assert construct in source
    assert "AidyShadowRuntime(session_factory)" not in source
    assert source.index(research_factory) < source.index(broker_gate)


@pytest.mark.asyncio
async def test_aidy_runtime_timeout_fails_research_flat_without_live_dependency(caplog) -> None:
    runtime = AidyShadowRuntime(
        object(),
        poll_seconds=60,
        startup_pass_limit=1,
        pass_timeout_seconds=0.01,
    )
    stop = asyncio.create_task(
        runtime._run(_SlowResolver(), None, None)
    )
    await asyncio.sleep(0.05)
    runtime._stopping.set()
    await stop
    assert "research pass skipped, live trading unchanged" in caplog.text


def test_broker_shadow_manager_cannot_start_second_aidy_resolver() -> None:
    source = (ROOT / "app" / "shadow_trading_v4.py").read_text(encoding="utf-8")
    assert "AidyShadowResolver" not in source
    assert "AidyMarketClient" not in source
    assert "super-signals-shadow-aidy-m1" not in source
