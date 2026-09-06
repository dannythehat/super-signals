from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.aidy_context_client import AidyCanonicalContext
from app.provider_context_attachment import (
    CONTRACT_VERSION,
    ContextAttachmentCandidate,
    ProviderContextAttachmentResolver,
    _digest,
)
from app.provider_aidy_context_join import ProviderAidyContextJoin
from app.aidy_shadow_runtime import AidyShadowRuntime

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0058_provider_aidy_context_attachment.py"


def _migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _joined() -> tuple[ContextAttachmentCandidate, ProviderAidyContextJoin]:
    signal_at = datetime(2026, 9, 6, 16, 0, tzinfo=UTC)
    profile_at = signal_at - timedelta(hours=2)
    candidate = ContextAttachmentCandidate(
        signal_id=uuid4(),
        source_id=uuid4(),
        message_id=uuid4(),
        signal_posted_at=signal_at,
        provider_profile_version_id=uuid4(),
        provider_profile_version_no=4,
        provider_profile_effective_at=profile_at,
    )
    aidy = AidyCanonicalContext(
        requested_as_of_utc=signal_at,
        context_as_of_utc=signal_at - timedelta(minutes=3),
        context_lag_seconds=180,
        context_hash="b" * 64,
        snapshot_id=str(uuid4()),
        snapshot_digest="a" * 64,
        snapshot_archive_key="gold/snapshots/2026/09/06/example.json",
        session={"computed_session_code": "new_york"},
        regime={"compound_regime_key": "trend|normal"},
        data_quality={"state": "good"},
        market={"quote_context": {"mid": "3500"}},
        provenance={
            "private_forward_only": True,
            "live_money_execution_allowed": False,
        },
    )
    joined = ProviderAidyContextJoin(
        signal_posted_at=signal_at,
        provider_profile_version_id=str(candidate.provider_profile_version_id),
        provider_profile_version_no=candidate.provider_profile_version_no,
        provider_profile_effective_at=profile_at,
        aidy=aidy,
    )
    return candidate, joined


def test_day10_migration_is_single_forward_chain_from_day8() -> None:
    source = _migration_source()
    assert 'revision: str = "0058_provider_aidy_context"' in source
    assert 'down_revision: str | None = "0057_provider_pit_boundary"' in source
    assert len("0058_provider_aidy_context") <= 32


def test_day10_database_enforces_both_temporal_boundaries() -> None:
    source = _migration_source()
    assert "provider_profile_effective_at <= signal_posted_at" in source
    assert "aidy_requested_as_of_utc = signal_posted_at" in source
    assert "aidy_context_as_of_utc <= signal_posted_at" in source
    assert "aidy_context_lag_seconds >= 0 AND aidy_context_lag_seconds <= 600" in source
    assert "v.effective_at <= NEW.signal_posted_at" in source
    assert "provider_profile_pit_status='resolved'" in source
    assert "provider context point-in-time boundary invalid" in source


def test_day10_attachment_is_append_only_and_one_row_per_signal() -> None:
    source = _migration_source()
    assert 'sa.UniqueConstraint("signal_id"' in source
    assert "BEFORE UPDATE OR DELETE ON provider_signal_context_attachments" in source
    assert "provider signal context attachments are immutable" in source
    assert "ON DELETE CASCADE" not in source


def test_day10_database_denies_live_money_authority() -> None:
    source = _migration_source()
    assert "private_forward_only" in source
    assert "live_money_execution_allowed" in source
    assert "= false" in source


def test_day10_payload_freezes_exact_provider_and_aidy_identity_deterministically() -> None:
    candidate, joined = _joined()
    payload = ProviderContextAttachmentResolver._payload(candidate, joined)
    again = ProviderContextAttachmentResolver._payload(candidate, joined)
    assert payload == again
    assert _digest(payload) == _digest(again)
    assert len(_digest(payload)) == 64
    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["signal_posted_at"] == candidate.signal_posted_at.isoformat()
    assert payload["provider_profile"]["version_id"] == str(candidate.provider_profile_version_id)
    assert payload["aidy"]["requested_as_of_utc"] == candidate.signal_posted_at.isoformat()
    assert payload["aidy"]["context_hash"] == joined.aidy.context_hash
    assert payload["aidy"]["snapshot"]["digest"] == joined.aidy.snapshot_digest
    assert payload["point_in_time_clean"] is True
    assert payload["research_only"] is True
    assert payload["live_money_execution_allowed"] is False


def test_day10_candidate_query_is_resolved_only_bounded_and_idempotent() -> None:
    source = inspect.getsource(ProviderContextAttachmentResolver)
    assert "provider_profile_pit_status='resolved'" in source
    assert "provider_signal_context_attachments" in source
    assert "NOT EXISTS" in source
    assert "LIMIT :limit" in source
    assert "ON CONFLICT (signal_id) DO NOTHING" in source
    assert "legacy_unresolvable" not in source


def test_day10_resolver_has_no_broker_or_execution_authority() -> None:
    source = (ROOT / "app" / "provider_context_attachment.py").read_text(encoding="utf-8")
    assert "metaapi" not in source.lower()
    assert "execution_dispatch" not in source
    assert "member" not in "\n".join(
        line for line in source.lower().splitlines() if line.lstrip().startswith(("from ", "import "))
    )
    assert "UPDATE shadow_trades" not in source
    assert "DELETE FROM shadow_trades" not in source


def test_day10_context_failures_are_isolated_from_m1_resolution() -> None:
    source = inspect.getsource(AidyShadowRuntime._run)
    market_call = source.index("market_resolver.resolve_once()")
    context_call = source.index("context_resolver.resolve_once()")
    assert market_call < context_call
    assert source.count("except Exception") >= 2
    assert "context attachment loop failed safely" in source
    assert "cannot block either market resolution or live signal routing" in source


def test_day10_runtime_is_app_owned_not_broker_owned() -> None:
    source = inspect.getsource(AidyShadowRuntime.start)
    assert "AidyContextClient.from_environment" in source
    assert "ProviderContextAttachmentResolver" in source
    assert "MetaApi" not in source
    assert "broker" not in source.lower()
