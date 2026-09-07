from pathlib import Path


def test_widened_diagnostic_delegates_to_existing_calibration_replay():
    source = Path("app/provider_day11_widened_diagnostic.py").read_text(encoding="utf-8")
    assert "from app.provider_day11_calibration_replay import replay_signal_calibration" in source
    assert "result = await replay_signal_calibration(" in source
    assert "DAY11_WIDENED_SIGNAL_COUNT = 82" in source
    assert "source_posted_at <= c.frozen_at" in source
    assert "live_money_execution_allowed\": False" in source
