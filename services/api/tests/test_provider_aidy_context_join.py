from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.aidy_context_client import AidyCanonicalContext
from app.provider_aidy_context_join import (
    ProviderContextJoinBlocked,
    join_provider_to_aidy_context,
)

ROOT = Path(__file__).resolve().parents[2]


class _Client:
    def __init__(self, context: AidyCanonicalContext) -> None:
        self.context = context
        self.calls: list[datetime] = []

    async def fetch_context(self, *, as_of: datetime) -> AidyCanonicalContext:
        self.calls.append(as_of)
        return self.context


def _context(*, requested: datetime, context_at: datetime | None = None) -> AidyCanonicalContext:
    at = requested - timedelta(minutes=3) if context_at is None else context_at
    return AidyCanonicalContext(
        requested_as_of_utc=requested,
        context_as_of_utc=at,
        context_lag_seconds=int((requested - at).total_seconds()),
        context_hash="context-hash",
        snapshot_id="snapshot-1",
        snapshot_digest="a" * 64,
        snapshot_archive_key="gold/snapshots/2026/09/06/snapshot-1.json",
        session={"computed_session_code": "new_york"},
        regime={"compound_regime_key": "trend|normal"},
        data_quality={"state": "good"},
        market={"quote_context": {"mid": "3500"}},
        provenance={"private_forward_only": True, "live_money_execution_allowed": False},
    )


@pytest.mark.asyncio
async def test_join_combines_only_two_clean_pit_boundaries() -> None:
    signal = datetime(2026, 9, 6, 12, 5, tzinfo=UTC)
    client = _Client(_context(requested=signal))
    joined = await join_provider_to_aidy_context(
        client=client,  # type: ignore[arg-type]
        signal_posted_at=signal,
        provider_profile_pit_status="resolved",
        provider_profile_version_id="profile-version-7",
        provider_profile_version_no=7,
        provider_profile_effective_at=signal - timedelta(hours=1),
    )
    assert joined.point_in_time_clean is True
    assert joined.aidy.context_as_of_utc < signal
    assert client.calls == [signal]


@pytest.mark.asyncio
async def test_legacy_provider_row_is_blocked_before_aidy_call() -> None:
    signal = datetime(2026, 9, 6, 12, 5, tzinfo=UTC)
    client = _Client(_context(requested=signal))
    with pytest.raises(ProviderContextJoinBlocked, match="provider_profile_not_pit_resolved"):
        await join_provider_to_aidy_context(
            client=client,  # type: ignore[arg-type]
            signal_posted_at=signal,
            provider_profile_pit_status="legacy_unresolvable",
            provider_profile_version_id=None,
            provider_profile_version_no=None,
            provider_profile_effective_at=None,
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_future_provider_profile_is_blocked() -> None:
    signal = datetime(2026, 9, 6, 12, 5, tzinfo=UTC)
    client = _Client(_context(requested=signal))
    with pytest.raises(ProviderContextJoinBlocked, match="provider_profile_future_leak"):
        await join_provider_to_aidy_context(
            client=client,  # type: ignore[arg-type]
            signal_posted_at=signal,
            provider_profile_pit_status="resolved",
            provider_profile_version_id="profile-version-8",
            provider_profile_version_no=8,
            provider_profile_effective_at=signal + timedelta(seconds=1),
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_future_aidy_context_is_blocked() -> None:
    signal = datetime(2026, 9, 6, 12, 5, tzinfo=UTC)
    client = _Client(_context(requested=signal, context_at=signal + timedelta(seconds=1)))
    with pytest.raises(ProviderContextJoinBlocked, match="aidy_context_future_leak"):
        await join_provider_to_aidy_context(
            client=client,  # type: ignore[arg-type]
            signal_posted_at=signal,
            provider_profile_pit_status="resolved",
            provider_profile_version_id="profile-version-7",
            provider_profile_version_no=7,
            provider_profile_effective_at=signal - timedelta(hours=1),
        )


def test_day9_join_has_no_trade_persistence_or_broker_authority() -> None:
    join_source = (ROOT / "app" / "provider_aidy_context_join.py").read_text(encoding="utf-8")
    client_source = (ROOT / "app" / "aidy_context_client.py").read_text(encoding="utf-8")
    assert "Day 10 owns" in join_source
    assert "INSERT " not in join_source
    assert "UPDATE " not in join_source
    assert "DELETE " not in join_source
    assert "MetaAPI" not in join_source
    assert "broker" not in join_source.lower()
    assert '"live_money_execution_allowed"' in client_source
    assert '"/provider/context?' in client_source
