from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scalper_runtime_uses_aidy_m1_and_replays_old_blanket_exclusions():
    resolver=(ROOT/"app"/"aidy_shadow_resolver.py").read_text(encoding="utf-8")
    v4=(ROOT/"app"/"shadow_trading_v4.py").read_text(encoding="utf-8")
    v5=(ROOT/"app"/"shadow_trading_v5.py").read_text(encoding="utf-8")
    fairness=(ROOT/"app"/"provider_fairness.py").read_text(encoding="utf-8")
    assert '_SUPPORTED_STYLES = {"scalper", "intraday", "swing_or_sparse"}' in resolver
    assert "provider_style IN ('scalper','intraday','swing_or_sparse')" in resolver
    assert '"unsupported_style_scalper"' in resolver  # backlog is an allowed replay trigger
    assert 'aidy_m1_scalper_intrabar_sequence_ambiguous' in resolver
    assert '_prepare_scalper_m1_resolution_sync' in v4
    assert '_enforce_scalper_exclusion_sync' not in v4 + v5
    assert 'return "unsupported"' not in fairness


def test_phase_c_does_not_touch_live_execution_or_risk():
    migration=(ROOT/"migrations"/"versions"/"0077_enable_scalper_aidy_m1.py").read_text(encoding="utf-8")
    assert "outcome_pending_aidy_m1" in migration
    assert "provider_profile_pit_status = 'resolved'" in migration
    assert "aidy_original_geometry IS NOT NULL" in migration
    assert "live-risk" in migration
    assert "UPDATE accounts" not in migration
    assert "UPDATE user_trading_settings" not in migration
