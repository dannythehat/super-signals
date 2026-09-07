from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.aidy_market_client import AidyMarketClient
from app.provider_day11_calibration_acceptance import (
    DAY11_BASELINE_RUN_ID,
    DAY11_FROZEN_SIGNAL_COUNT,
)
from app.provider_day11_calibration_replay import CALIBRATION_EVIDENCE_DOMAIN

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 8, 20, 13, 43, tzinfo=UTC)


def _calibration_bar(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "open_time_utc": BASE.isoformat(),
        "first_observed_at": (BASE + timedelta(days=18)).isoformat(),
        "open": "4300",
        "high": "4301",
        "low": "4299",
        "close": "4300.5",
        "revision_index": 0,
        "payload_digest": "a" * 64,
        "source_kind": "calibration_backfill",
        "source_provider": "twelve_data",
        "pit_eligible": False,
        "research_only": True,
        "live_money_execution_allowed": False,
    }
    row.update(overrides)
    return row


def test_calibration_bar_is_explicitly_retrospective_and_never_pit_eligible() -> None:
    end = BASE + timedelta(minutes=1)
    bar = AidyMarketClient._calibration_bar(_calibration_bar(), start=BASE, end=end)
    assert bar.first_observed_at > end
    for field, invalid in (
        ("pit_eligible", True),
        ("research_only", False),
        ("live_money_execution_allowed", True),
        ("source_kind", "live"),
        ("source_provider", "other"),
    ):
        with pytest.raises(ValueError):
            AidyMarketClient._calibration_bar(_calibration_bar(**{field: invalid}), start=BASE, end=end)


def test_live_pit_bar_parser_still_rejects_late_observation() -> None:
    end = BASE + timedelta(minutes=1)
    row = _calibration_bar()
    for field in ("source_kind", "source_provider", "pit_eligible", "research_only", "live_money_execution_allowed"):
        row.pop(field)
    with pytest.raises(ValueError, match="first_observed_after_pit_cutoff"):
        AidyMarketClient._bar(row, start=BASE, end=end)


def test_acceptance_is_hard_bound_to_original_56_signal_production_run() -> None:
    assert str(DAY11_BASELINE_RUN_ID) == "c5fae236-a63f-4340-99e7-7890e08f782a"
    assert DAY11_FROZEN_SIGNAL_COUNT == 56
    source = (ROOT / "app" / "provider_day11_calibration_acceptance.py").read_text(encoding="utf-8")
    assert "provider_execution_reconciliation_samples" in source
    assert "WHERE run_id=:baseline_run_id" in source
    assert "DAY11_FROZEN_SIGNAL_COUNT = 56" in source
    assert "replay_signal_calibration" in source


def test_calibration_replay_never_calls_normal_pit_fetch_path() -> None:
    source = (ROOT / "app" / "provider_day11_calibration_replay.py").read_text(encoding="utf-8")
    assert CALIBRATION_EVIDENCE_DOMAIN == "calibration_backfill:twelve_data"
    assert "fetch_calibration_m1" in source
    assert ".fetch_m1(" not in source
    assert "live_money_execution_allowed" in source


def test_runtime_retires_day11_replay_after_verified_closure() -> None:
    startup = (ROOT.parents[1] / "scripts" / "render-start.sh").read_text(encoding="utf-8")
    assert "app.provider_day13_conditional" in startup
    assert "app.provider_day12_fingerprint" not in startup
    assert "app.provider_day11_widened_diagnostic" not in startup
    assert "app.provider_day11_calibration_acceptance" not in startup
    assert "python -m app.provider_day11_acceptance" not in startup
