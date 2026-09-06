from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.provider_aware_ai_pipeline import ProviderAwareProductionAiPipeline
from app.provider_profile_pit import ProviderProfilePIT, resolve_provider_profile_as_of
from app.shadow_trading_service_v4 import ShadowTradeService


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0057_provider_pit_boundary.py"


def _migration_source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_day8_migration_is_single_forward_chain_from_day7() -> None:
    source = _migration_source()
    assert 'revision: str = "0057_provider_pit_boundary"' in source
    assert 'down_revision: str | None = "0056_provider_profile_versions"' in source
    assert len("0057_provider_pit_boundary") <= 32


def test_day8_legacy_rows_are_never_backfilled_from_future_profiles() -> None:
    source = _migration_source()
    assert "pv.effective_at <= t.signal_posted_at" in source
    assert "WHEN m.version_id IS NULL THEN 'legacy_unresolvable'" in source
    assert "WHEN m.version_id IS NULL THEN 'unknown'" in source
    assert "legacy_profile_unresolvable" in source
    assert "WHEN m.version_id IS NULL THEN false" in source


def test_day8_database_blocks_unproven_score_eligibility() -> None:
    source = _migration_source()
    assert "NOT score_eligible OR provider_profile_pit_status='resolved'" in source
    assert "legacy provider profile state cannot be score eligible" in source
    assert "v.effective_at <= NEW.signal_posted_at" in source
    assert "trg_shadow_trade_provider_profile_pit" in source


def test_day8_ai_context_uses_immutable_as_of_profile_only() -> None:
    source = inspect.getsource(ProviderAwareProductionAiPipeline._source_context)
    assert "resolve_provider_profile_as_of" in source
    assert "provider_research_profiles" not in source
    assert "_adaptive_profiles.get" not in source
    assert "provider_research_profile_pit" in source
    assert "if profile is None" in source


def test_day8_shadow_enrollment_stamps_exact_profile_provenance() -> None:
    source = inspect.getsource(ShadowTradeService.record_signal)
    assert "resolve_provider_profile_as_of" in source
    assert "provider_research_profiles" not in source
    assert "provider_profile_version_id" in source
    assert "provider_profile_version_no" in source
    assert "provider_profile_effective_at" in source
    assert "provider_profile_pit_status" in source
    assert "legacy_profile_unresolvable" in source


def test_day8_pit_resolver_has_no_mutable_profile_fallback() -> None:
    source = inspect.getsource(resolve_provider_profile_as_of)
    assert "provider_research_profile_versions" in source
    assert "effective_at <= :as_of" in source
    assert "provider_research_profiles" not in source
    assert "return None" in source


def test_day8_profile_context_reads_only_frozen_snapshot() -> None:
    effective = datetime(2026, 9, 6, 14, 39, tzinfo=UTC)
    profile = ProviderProfilePIT(
        id=uuid4(),
        source_id=uuid4(),
        version_no=3,
        effective_at=effective,
        snapshot_fingerprint="0" * 32,
        source_identity={"status": "shadow"},
        profile_snapshot={
            "style": "intraday",
            "interpretation_readiness": "0.75",
            "profile_metadata": {
                "adaptive_v1": {
                    "language": {
                        "entry_bucket": "zone",
                        "traits": {"uses_runner_language": True},
                    }
                },
                "management_intensity": 0.4,
            },
        },
    )
    assert profile.style == "intraday"
    assert profile.interpretation_readiness == 0.75
    assert profile.adaptive_language_profile["entry_bucket"] == "zone"
    assert profile.communication_traits["management_intensity"] == 0.4
