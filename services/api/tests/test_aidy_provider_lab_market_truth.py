from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.aidy_market_client import AIDY_QUOTE_MODE, AidyM1Bar
from app.aidy_shadow_resolver import LegState, resolve_bars

ROOT = Path(__file__).resolve().parents[1]


def _trade(*, side: str = "BUY", order: str = "market") -> dict[str, object]:
    return {
        "side": side,
        "entry_order_type": order,
        "entry_low": Decimal("100"),
        "entry_high": Decimal("100"),
        "initial_stop": Decimal("95") if side == "BUY" else Decimal("105"),
        "status": "pending",
        "entry_price": None,
        "opened_at": None,
        "aidy_effective_stop": None,
        "aidy_m1_cursor_at": None,
        "closed_at": None,
        "close_reason": None,
        "score_exclusion_reason": "market_data_not_observed",
        "aidy_resolution_note": None,
        "last_price": None,
        "max_price": None,
        "min_price": None,
    }


def _bar(*, minute: int, high: str, low: str, close: str = "100") -> AidyM1Bar:
    return AidyM1Bar(
        open_time_utc=datetime(2026, 9, 4, 10, minute, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def test_same_m1_bar_sl_and_tp_is_worst_case_stop_and_scoreable() -> None:
    legs = [
        LegState(id=uuid4(), tp_index=1, target=Decimal("105"), is_runner=False),
        LegState(id=uuid4(), tp_index=2, target=Decimal("110"), is_runner=False),
    ]
    state = resolve_bars(
        trade=_trade(),
        legs=legs,
        events=[],
        bars=[_bar(minute=1, high="106", low="94")],
        reset_from_signal=True,
    )
    assert state.status == "closed"
    assert state.score_eligible is True
    assert state.close_reason == "aidy_m1_ambiguous_worst_case_stop"
    assert state.note == "within_bar_sl_and_tp_touched_stop_assumed_first"
    assert all(leg.exit_price == Decimal("95") for leg in state.legs)
    assert all(leg.quality_r == Decimal("-1") for leg in state.legs)


def test_clean_m1_target_resolution_becomes_scoreable() -> None:
    legs = [LegState(id=uuid4(), tp_index=1, target=Decimal("105"), is_runner=False)]
    state = resolve_bars(
        trade=_trade(),
        legs=legs,
        events=[],
        bars=[_bar(minute=1, high="106", low="99", close="105")],
        reset_from_signal=True,
    )
    assert state.status == "closed"
    assert state.score_eligible is True
    assert state.close_reason == "all_targets_hit"
    assert state.legs[0].exit_reason == "target"
    assert state.legs[0].quality_r == Decimal("1")


def test_zone_entry_does_not_claim_same_bar_target_without_sequence_evidence() -> None:
    trade = _trade(order="zone")
    trade["entry_low"] = Decimal("99")
    trade["entry_high"] = Decimal("101")
    legs = [LegState(id=uuid4(), tp_index=1, target=Decimal("105"), is_runner=False)]
    state = resolve_bars(
        trade=trade,
        legs=legs,
        events=[],
        bars=[
            _bar(minute=1, high="106", low="100", close="104"),
            _bar(minute=2, high="106", low="102", close="105"),
        ],
        reset_from_signal=True,
    )
    assert state.status == "closed"
    assert state.legs[0].closed_at == datetime(2026, 9, 4, 10, 3, tzinfo=UTC)


def test_break_even_inside_price_active_bar_fails_closed() -> None:
    trade = _trade()
    legs = [LegState(id=uuid4(), tp_index=1, target=Decimal("110"), is_runner=False)]
    event = {
        "event_type": "break_even",
        "occurred_at": datetime(2026, 9, 4, 10, 2, 30, tzinfo=UTC),
        "aggregate_result": {},
    }
    state = resolve_bars(
        trade=trade,
        legs=legs,
        events=[event],
        bars=[
            _bar(minute=1, high="103", low="99", close="102"),
            _bar(minute=2, high="104", low="99", close="100"),
        ],
        reset_from_signal=True,
    )
    assert state.status == "open"
    assert state.score_eligible is False
    assert state.exclusion_reason == "aidy_m1_management_bar_ambiguous"


def test_schema_migration_adds_aidy_mode_and_hard_excludes_scalpers() -> None:
    migration = (
        ROOT / "migrations" / "versions" / "0055_aidy_provider_lab_market_truth.py"
    ).read_text(encoding="utf-8")
    assert "'aidy_m1'" in migration
    assert "unsupported_style_scalper" in migration
    assert "aidy_m1_cursor_at" in migration
    assert "aidy_effective_stop" in migration
    assert AIDY_QUOTE_MODE == "aidy_m1"
