from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.aidy_context_client import AidyContextClient, AidyContextTerminalMiss
from app.aidy_shadow_runtime import AidyShadowRuntime
from app.provider_context_attachment import (
    ContextAttachmentCandidate,
    ProviderContextAttachmentResolver,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0064_provider_context_terminal_miss.py"


def _candidate() -> ContextAttachmentCandidate:
    signal_at = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    return ContextAttachmentCandidate(
        signal_id=uuid4(),
        source_id=uuid4(),
        message_id=uuid4(),
        signal_posted_at=signal_at,
        provider_profile_version_id=uuid4(),
        provider_profile_version_no=2,
        provider_profile_effective_at=signal_at - timedelta(hours=1),
    )


def test_terminal_miss_migration_is_forward_only_research_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0064_provider_context_terminal"' in source
    assert 'down_revision: str | None = "0063_provider_day14_governance"' in source
    assert "provider_signal_context_terminal_misses" in source
    assert "reason = 'pit_context_stale'" in source
    assert 'sa.UniqueConstraint("signal_id"' in source
    assert "research_only" in source
    assert "NOT live_money_execution_allowed" in source
    assert "BEFORE UPDATE OR DELETE" in source
    assert "terminal misses are immutable" in source


def test_candidate_query_excludes_both_successes_and_terminal_misses() -> None:
    source = inspect.getsource(ProviderContextAttachmentResolver._candidates)
    assert "provider_signal_context_attachments" in source
    assert "provider_signal_context_terminal_misses" in source
    assert source.count("NOT EXISTS") >= 2
    assert "LIMIT :limit" in source


def test_candidate_query_uses_canonical_signal_message_provenance() -> None:
    source = inspect.getsource(ProviderContextAttachmentResolver._candidates)
    assert "JOIN signals s ON s.id=t.signal_id" in source
    assert "JOIN messages m ON m.id=s.source_message_id" in source
    assert "m.source_id AS source_id" in source
    assert "s.source_message_id AS message_id" in source
    assert "s.source_posted_at AS signal_posted_at" in source
    assert "t.source_id=m.source_id" in source
    assert "t.message_id=s.source_message_id" in source
    assert "t.provider_profile_effective_at <= s.source_posted_at" in source


def test_pit_stale_is_classified_before_generic_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status_code = 409

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "ok": False,
                "error": "pit_context_stale",
                "context_lag_seconds": 900,
                "max_context_lag_seconds": 600,
            }

        @staticmethod
        def raise_for_status() -> None:
            raise AssertionError("terminal stale must be classified before raise_for_status")

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, *args: object, **kwargs: object) -> FakeResponse:
            return FakeResponse()

    import app.aidy_context_client as module

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeAsyncClient)
    client = AidyContextClient(base_url="https://aidy.test", bearer_token="token")
    with pytest.raises(AidyContextTerminalMiss) as caught:
        asyncio.run(client.fetch_context(as_of=datetime(2026, 9, 7, 12, 0, tzinfo=UTC)))
    assert caught.value.reason == "pit_context_stale"
    assert caught.value.payload["context_lag_seconds"] == 900


def test_terminal_miss_is_persisted_once_without_counting_as_retry_failure() -> None:
    candidate = _candidate()

    class TerminalClient:
        async def fetch_context(self, *, as_of: datetime) -> object:
            raise AidyContextTerminalMiss(
                "pit_context_stale",
                payload={
                    "ok": False,
                    "error": "pit_context_stale",
                    "requested_as_of_utc": as_of.isoformat(),
                },
            )

    resolver = ProviderContextAttachmentResolver(object(), TerminalClient())  # type: ignore[arg-type]
    resolver._candidates = lambda: [candidate]  # type: ignore[method-assign]
    persisted: list[str] = []

    def persist_terminal(candidate_arg: ContextAttachmentCandidate, miss: AidyContextTerminalMiss) -> bool:
        assert candidate_arg.signal_id == candidate.signal_id
        persisted.append(miss.reason)
        return True

    resolver._persist_terminal_miss = persist_terminal  # type: ignore[method-assign]
    attached, failures = asyncio.run(resolver.resolve_once())
    assert attached == 0
    assert failures == 0
    assert resolver.last_terminal_misses == 1
    assert persisted == ["pit_context_stale"]


def test_transient_context_failure_remains_retryable() -> None:
    candidate = _candidate()

    class TransientClient:
        async def fetch_context(self, *, as_of: datetime) -> object:
            raise RuntimeError("temporary_network_or_503")

    resolver = ProviderContextAttachmentResolver(object(), TransientClient())  # type: ignore[arg-type]
    resolver._candidates = lambda: [candidate]  # type: ignore[method-assign]
    attached, failures = asyncio.run(resolver.resolve_once())
    assert attached == 0
    assert failures == 1
    assert resolver.last_terminal_misses == 0


def test_startup_drain_counts_terminal_progress_without_changing_live_routing() -> None:
    source = inspect.getsource(AidyShadowRuntime._run)
    assert "terminal_misses = context_resolver.last_terminal_misses" in source
    assert "processed > 0 or attached > 0 or terminal_misses > 0" in source
    assert "cannot block either market resolution or live signal routing" in source
    assert "broker" in source.lower()
    assert "execution_dispatch" not in source
