from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_day13_runtime_uses_canonical_aidy_context_column() -> None:
    source = (ROOT / "app" / "provider_day13_runtime.py").read_text(encoding="utf-8")
    assert "a.aidy_context_as_of_utc" in source
    assert "a.context_as_of_utc" not in source
    assert "s.status='shadow'" in source
    assert "t.score_eligible" in source
    assert "t.provider_profile_pit_status='resolved'" in source
    assert "broker_deals" not in source
    assert "metaapi" not in source.casefold()


def test_render_start_uses_day13_schema_compatible_runner() -> None:
    startup = (ROOT.parents[1] / "scripts" / "render-start.sh").read_text(encoding="utf-8")
    assert "python -m app.provider_day13_runtime" in startup
    assert "python -m app.provider_day13_conditional &" not in startup
