from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.aidy_event_liquidity_execution import build_event_liquidity_execution_context


NOW = datetime(2026, 9, 18, 14, 0, tzinfo=UTC)


def _calibration(**overrides):
    base = {
        "entry_slippage_samples": 100,
        "exit_slippage_samples": 100,
        "contract_value_samples": 100,
        "cash_charge_samples": 100,
        "entry_adverse_p50_points": 0,
        "entry_adverse_p95_points": 1.1,
        "exit_adverse_p50_points": 0,
        "exit_adverse_p95_points": 0.1,
        "usd_per_point_per_lot_p50": 100,
        "cash_charge_per_lot_p50_usd": 0,
        "cash_charge_per_lot_p95_usd": 0,
        "evidence_as_of_utc": (NOW - timedelta(minutes=5)).isoformat(),
    }
    base.update(overrides)
    return base


def test_build2_surfaces_event_liquidity_late_entry_and_calibrated_execution() -> None:
    market = {
        "session": "london_new_york_overlap",
        "event_timing": "clear_current_window",
        "quote_state": "known",
        "quote_freshness": "fresh",
        "market": {
            "quote_context": {
                "mid": "4275",
                "quote_time": (NOW - timedelta(seconds=30)).isoformat(),
                "quote_age_seconds": 30,
                "spread": "0.20",
            }
        },
        "todays_scheduled_events": [
            {
                "title": "US CPI",
                "country": "USD",
                "impact": "High",
                "time_utc": (NOW + timedelta(minutes=45)).isoformat(),
                "forecast": "2.5%",
                "previous": "2.7%",
            }
        ],
    }
    result = build_event_liquidity_execution_context(
        signal_posted_at=NOW,
        side="BUY",
        entry_low="4260",
        entry_high="4265",
        stop_loss="4255",
        take_profits=["4268", "4271", "4278"],
        market_context=market,
        execution_calibration=_calibration(),
    )
    assert result["event"]["nearest_scheduled_event"]["minutes_from_signal"] == 45
    assert result["event"]["directional_prediction_allowed"] is False
    assert result["event"]["realized_event_outcome_available"] is False
    assert result["liquidity"]["mid"] == "4275"
    assert result["execution_geometry"]["entry_zone_relation_to_mid"] == "above_entry_zone"
    assert result["execution_geometry"]["targets_already_crossed_at_quote"] == 2
    assert result["broker_execution_calibration"]["status"] == "engineering_calibrated"
    assert result["live_money_execution_allowed"] is False


def test_build2_rejects_future_quote_and_future_execution_evidence() -> None:
    market = {
        "market": {
            "quote_context": {
                "mid": "4275",
                "quote_time": (NOW + timedelta(seconds=1)).isoformat(),
            }
        }
    }
    result = build_event_liquidity_execution_context(
        signal_posted_at=NOW,
        side="SELL",
        entry_low="4280",
        entry_high="4285",
        stop_loss="4295",
        take_profits=["4270"],
        market_context=market,
        execution_calibration=_calibration(
            evidence_as_of_utc=(NOW + timedelta(seconds=1)).isoformat()
        ),
    )
    assert result["liquidity"]["quote_state"] == "invalid_future_quote"
    assert result["liquidity"]["mid"] is None
    assert result["execution_geometry"]["entry_zone_relation_to_mid"] == "unknown"
    assert result["broker_execution_calibration"]["status"] == "invalid_future_evidence"


def test_build2_keeps_thin_execution_calibration_unknown() -> None:
    result = build_event_liquidity_execution_context(
        signal_posted_at=NOW,
        side="BUY",
        entry_low="4200",
        entry_high="4201",
        stop_loss="4190",
        take_profits=["4210"],
        market_context={},
        execution_calibration=_calibration(entry_slippage_samples=29),
    )
    assert result["broker_execution_calibration"]["status"] == "insufficient_samples"
    assert result["event"]["event_timing"] == "unknown"
    assert result["event"]["day_map_available"] is False
