from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0066_provider_shadow_enrollment_audit.py"
V5 = ROOT / "app" / "shadow_trading_v5.py"
V6 = ROOT / "app" / "shadow_trading_v6.py"
SURFACE = ROOT / "app" / "shadow_trading.py"


def test_enrollment_audit_is_total_research_only_boundary() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0066_shadow_enrollment_audit"' in source
    assert 'down_revision: str | None = "0065_provider_metadata_guard"' in source
    assert "provider_shadow_enrollment_audit" in source
    assert "status IN ('pending','enrolled','excluded')" in source
    assert "CHECK (research_only)" in source
    assert "CHECK (NOT live_money_execution_allowed)" in source
    assert "WHEN st.signal_id IS NOT NULL THEN 'enrolled' ELSE 'pending'" in source
    assert "legacy_bare_profile_no_fresh_quote" not in source


def test_runtime_has_no_silent_accepted_signal_state() -> None:
    source = V5.read_text(encoding="utf-8")
    assert "_sync_enrollment_audit_sync" in source
    assert "_repair_structured_pending_sync" in source
    assert "_fresh_bare_candidates_sync" in source
    assert "_expire_unresolved_pending_sync" in source
    assert "enrollment_evidence_unavailable_after_90s" in source
    assert "accepted_without_benchmarkable_geometry" in source
    assert "structured_repair_enrolled" in source


def test_same_message_completion_can_create_first_shadow_row_only() -> None:
    source = V5.read_text(encoding="utf-8")
    block = source[source.index("def _insert_revised_structured_signal_sync"):source.index("def _fresh_bare_candidates_sync")]
    assert "if self._shadow_exists_sync(signal_id):\n            return False" in block
    assert "source_revision_index" in block
    assert "ON CONFLICT (signal_id,entry_index) DO NOTHING" in block
    assert "provider_profile_pit_status" in block
    assert "live_money" not in block.lower()


def test_bare_now_enrollment_uses_fresh_executable_quote_and_existing_policy() -> None:
    source = V5.read_text(encoding="utf-8")
    assert "_MAX_FRESH_BARE_AGE_SECONDS = 90" in source
    assert "_MAX_SCORABLE_BARE_ENTRY_DELAY_MS = 20_000" in source
    assert "entry = ask if side == \"BUY\" else bid" in source
    assert "STOP_LOSS_DISTANCE" in source
    assert "TAKE_PROFIT_DISTANCE" in source
    assert '"entry_source": "first_fresh_public_executable_quote"' in source
    assert '"provider_signal_posted_at": provider_posted_at.isoformat()' in source
    assert '"enrollment_time": observed_at' in source
    assert "bare_profile_entry_delay_over_scoring_gate" in source


def test_v6_postgres_sync_avoids_invalid_target_lateral_reference() -> None:
    source = V6.read_text(encoding="utf-8")
    assert "FROM LATERAL" not in source
    assert "WHERE st.signal_id=a.signal_id" in source
    assert "EXISTS(" in source


def test_public_surface_uses_complete_runtime_without_changing_shadow_service() -> None:
    source = SURFACE.read_text(encoding="utf-8")
    assert "from app.shadow_trading_service_v4 import ShadowTradeService" in source
    assert "from app.shadow_trading_v6 import ShadowTradeManager" in source
