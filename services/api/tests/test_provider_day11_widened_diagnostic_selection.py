from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_widened_selection_does_not_filter_on_realised_outcomes():
    source = (ROOT / "app" / "provider_day11_widened_diagnostic.py").read_text(encoding="utf-8")
    selection = source.split("WITH baseline_sources AS (", 1)[1].split("ORDER BY source_id,source_posted_at,id", 1)[0]
    assert "bd.profit" not in selection.lower()
    assert "paper_r" not in selection
    assert "broker_r" not in selection
    assert "abs_r_delta" not in selection
    assert "lifecycle_matches" not in selection
