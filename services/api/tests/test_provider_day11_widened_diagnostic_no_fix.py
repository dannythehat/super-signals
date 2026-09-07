from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_diagnostic_is_selection_and_reporting_only():
    source = (ROOT / "app" / "provider_day11_widened_diagnostic.py").read_text(encoding="utf-8")
    assert "replay_signal_calibration" in source
    assert "_broker_truth" not in source
    assert "_geometry_for_entry" not in source
    assert "_allocation_pairs" not in source
