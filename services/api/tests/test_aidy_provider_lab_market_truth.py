from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.aidy_market_client import AidyM1Bar, AidyM1Window, AidyMarketClient
from app.aidy_shadow_resolver import (
    OriginalGeometry,
    _initial_state,
    contiguous_bars,
    lifecycle_watermark,
    replay_bars,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


def _bar(minute: int, *, o: str = "100", high: str = "100", low: str = "100", close: str = "100", rev: int = 1) -> AidyM1Bar:
    opened = BASE + timedelta(minutes=minute)
    return AidyM1Bar(
        open_time_utc=opened,
        open=Decimal(o),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        revision_index=rev,
        first_observed_at=opened + timedelta(minutes=1, seconds=5),
        payload_digest=(f"{rev:x}" * 64)[:64],
    )


def _geometry(
    side: str = "BUY",
    order: str = "market",
    entry_low: str = "100",
    entry_high: str = "100",
    stop: str | None = None,
    targets: tuple[str, ...] = ("105",),
    *,
    runner: bool = False,
    entry_index: int = 1,
) -> OriginalGeometry:
    if stop is None:
        stop = "95" if side == "BUY" else "105"
    mapped = {index: Decimal(value) for index, value in enumerate(targets, start=1)}
    runners: set[int] = set()
    if runner:
        idx = len(mapped) + 1
        mapped[idx] = None
        runners.add(idx)
    return OriginalGeometry(
        side=side,
        order_type=order,
        entry_low=Decimal(entry_low),
        entry_high=Decimal(entry_high),
        initial_stop=Decimal(stop),
        entry_index=entry_index,
        targets=mapped,
        runners=frozenset(runners),
    )


def _state(g: OriginalGeometry, *, posted: datetime = BASE):
    return _initial_state(
        geometry=g,
        leg_ids={index: uuid4() for index in g.targets},
        signal_posted_at=posted,
        lifecycle_mark="m0",
    )


def _event(minute: int, action: dict[str, object], *, seconds: int = 0, key: str = "e") -> dict[str, object]:
    when = BASE + timedelta(minutes=minute, seconds=seconds)
    return {
        "id": uuid4(),
        "event_key": f"{key}-{uuid4()}",
        "event_type": "provider_update",
        "occurred_at": when,
        "created_at": when + timedelta(seconds=1),
        "aggregate_result": {"revised_instruction": {"management_actions": [action]}},
    }


def _run(g: OriginalGeometry, bars: list[AidyM1Bar], *, events=None, posted=BASE, state=None, full=True):
    state = state or _state(g, posted=posted)
    return replay_bars(
        state=state,
        geometry=g,
        events=list(events or []),
        bars=bars,
        signal_posted_at=posted,
        sibling_entries=[(g.entry_index, g.entry_high if g.side == "BUY" else g.entry_low)],
        full_replay=full,
    )


def test_1_buy_sell_tp_sl_signs_and_neutral_bar() -> None:
    buy = _geometry("BUY", targets=("105",))
    state = _run(buy, [_bar(0, high="104", low="96")])
    assert state.status == "open"
    state = _run(buy, [_bar(1, high="106", low="99")], state=state, full=False)
    assert state.legs[0].exit_reason == "target"
    sell = _geometry("SELL", stop="105", targets=("95",))
    state = _run(sell, [_bar(0, high="104", low="96")])
    assert state.status == "open"
    state = _run(sell, [_bar(1, high="101", low="94")], state=state, full=False)
    assert state.legs[0].exit_reason == "target"


def test_2_same_bar_worst_case_is_symmetric_and_terminal() -> None:
    for side, stop, target in (("BUY", "95", "105"), ("SELL", "105", "95")):
        g = _geometry(side, stop=stop, targets=(target,))
        state = _run(g, [_bar(0, high="106", low="94")])
        assert state.close_reason == "aidy_m1_ambiguous_worst_case_stop"
        assert state.score_eligible is True
        assert state.legs[0].realized_r == Decimal("-1")
        digest = state.evidence_digest
        state2 = _run(g, [_bar(1, high="120", low="80")], state=state, full=False)
        assert state2.close_reason == state.close_reason
        assert state2.evidence_digest == digest


def test_3_zone_fill_and_entry_bar_ambiguity() -> None:
    buy = _geometry("BUY", "zone", "99", "101", targets=("110",))
    state = _run(buy, [_bar(0, o="100", high="100", low="99.5", close="100")])
    assert state.entry_price == Decimal("100")
    sell = _geometry("SELL", "zone", "99", "101", stop="105", targets=("90",))
    state = _run(sell, [_bar(0, o="100", high="100.5", low="100", close="100")])
    assert state.entry_price == Decimal("100")
    entry_stop = _geometry("BUY", "buy_limit", "100", "100", targets=("110",))
    state = _run(entry_stop, [_bar(0, o="102", high="102", low="94", close="99")])
    assert state.score_block_reason == "aidy_m1_entry_stop_sequence_ambiguous"
    entry_tp = _geometry("BUY", "buy_limit", "100", "100", targets=("105",))
    state = _run(entry_tp, [_bar(0, o="102", high="106", low="99", close="104")])
    assert state.score_block_reason == "aidy_m1_entry_target_sequence_ambiguous"


@pytest.mark.parametrize(
    ("side", "order", "open_price"),
    [("BUY", "buy_limit", "99"), ("SELL", "sell_limit", "101"), ("BUY", "buy_stop", "101"), ("SELL", "sell_stop", "99")],
)
def test_3_pending_order_gap_semantics_are_symmetric(side: str, order: str, open_price: str) -> None:
    g = _geometry(
        side,
        order,
        "100",
        "100",
        stop="90" if side == "BUY" else "110",
        targets=("120",) if side == "BUY" else ("80",),
    )
    state = _run(g, [_bar(0, o=open_price, high=str(Decimal(open_price) + 1), low=str(Decimal(open_price) - 1), close=open_price)])
    assert state.entry_price == Decimal(open_price)


def test_4_multi_tp_runner_and_partial_r_do_not_double_count() -> None:
    g = _geometry("BUY", targets=("105", "110"), runner=True)
    half = _event(1, {"type": "close_half", "target": "tp1", "value": "103"})
    state = _run(
        g,
        [_bar(0, high="104", low="99"), _bar(1, high="104", low="101"), _bar(2, high="106", low="101"), _bar(3, high="111", low="104"), _bar(4, high="109", low="94")],
        events=[half],
    )
    assert state.legs[0].realized_r == Decimal("0.8")
    assert state.legs[1].realized_r == Decimal("2")
    assert state.legs[2].realized_r == Decimal("-1")
    assert all(leg.remaining_fraction == 0 for leg in state.legs)
    assert sum(leg.realized_r for leg in state.legs) == Decimal("1.8")


def test_5_original_tp_is_replayed_until_timestamped_edit() -> None:
    g = _geometry("BUY", targets=("105",))
    edit = _event(2, {"type": "move_take_profit", "target": "tp1", "value": "110"})
    state = _run(g, [_bar(0, high="104", low="99"), _bar(1, high="106", low="101")], events=[edit])
    assert state.terminal is True
    assert state.legs[0].exit_price == Decimal("105")


def test_5_evidence_provenance_and_digest() -> None:
    g = _geometry("BUY", targets=("105",))
    state = _run(g, [_bar(0, high="104", low="99", rev=2), _bar(1, high="106", low="101", rev=3)])
    assert state.evidence_digest is not None and len(state.evidence_digest) == 64
    target_items = [item for item in state.evidence_ledger if item["kind"] == "target"]
    assert target_items[0]["bar"]["revision_index"] == 3
    assert target_items[0]["bar"]["first_observed_at"].endswith("+00:00")
    assert len(target_items[0]["bar"]["payload_digest"]) == 64


def test_6_signal_minute_policy_quarantines_partial_minute_activity() -> None:
    posted = BASE + timedelta(seconds=30)
    g = _geometry("BUY", targets=("105",))
    state = _run(g, [_bar(0, high="106", low="99")], posted=posted)
    assert state.score_block_reason == "aidy_m1_signal_minute_ambiguous"


def test_6_market_boundary_rejects_naive_nonminute_and_invalid_ohlc() -> None:
    start, end = BASE, BASE + timedelta(minutes=2)
    raw = {
        "open_time_utc": "2026-09-04T10:00:00",
        "first_observed_at": "2026-09-04T10:01:05+00:00",
        "open": "100", "high": "101", "low": "99", "close": "100",
        "revision_index": 1, "payload_digest": "a" * 64,
    }
    with pytest.raises(ValueError, match="timezone_required"):
        AidyMarketClient._bar(raw, start=start, end=end)
    raw["open_time_utc"] = "2026-09-04T10:00:30+00:00"
    with pytest.raises(ValueError, match="minute_aligned"):
        AidyMarketClient._bar(raw, start=start, end=end)
    raw["open_time_utc"] = "2026-09-04T10:00:00+00:00"
    raw["low"] = "102"
    with pytest.raises(ValueError, match="invalid_ohlc_geometry"):
        AidyMarketClient._bar(raw, start=start, end=end)


def test_7_continuity_stops_at_first_missing_expected_minute() -> None:
    expected = tuple(BASE + timedelta(minutes=i) for i in range(4))
    window = AidyM1Window(BASE, BASE + timedelta(minutes=4), (_bar(0), _bar(2), _bar(3)), expected, (expected[1],))
    usable, gap = contiguous_bars(window, after_cursor=None)
    assert [bar.open_time_utc for bar in usable] == [expected[0]]
    assert gap == expected[1]


def test_7_lifecycle_watermark_detects_late_old_event() -> None:
    late = _event(1, {"type": "move_to_break_even", "target": "all", "value": None}, key="late")
    first = _event(3, {"type": "move_to_break_even", "target": "all", "value": None}, key="first")
    late["created_at"] = BASE + timedelta(minutes=4)
    # The applied prefix is chronological, not append-time or market-cursor derived.
    assert lifecycle_watermark([first]) != lifecycle_watermark([first, late])
    assert lifecycle_watermark([first]) != lifecycle_watermark([late])


def test_7_lifecycle_applied_prefix_advances_only_when_event_is_consumed() -> None:
    g = _geometry("BUY", targets=("120",))
    event = _event(3, {"type": "move_to_break_even", "target": "all", "value": None}, key="future")
    state = _run(g, [_bar(0, high="101", low="99"), _bar(1, high="102", low="99")], events=[event])
    assert state.market_cursor == BASE + timedelta(minutes=1)
    assert state.lifecycle_applied_count == 0
    state = _run(
        g,
        [_bar(2, high="102", low="99"), _bar(3, high="102", low="101")],
        events=[event],
        state=state,
        full=False,
    )
    assert state.lifecycle_applied_count == 1
    assert state.effective_stop == Decimal("100")


def test_8_ambiguity_is_sticky_and_terminal() -> None:
    g = _geometry("BUY", "buy_limit", "100", "100", targets=("110",))
    state = _run(g, [_bar(0, high="102", low="94")])
    assert state.score_block_reason == "aidy_m1_entry_stop_sequence_ambiguous"
    reason = state.score_block_reason
    state = _run(g, [_bar(1, high="120", low="99")], state=state, full=False)
    assert state.score_block_reason == reason
    assert state.score_eligible is False


def test_10_management_model_be_sl_tp_close_partial_runner_scope() -> None:
    g = _geometry("BUY", targets=("105", "110"), runner=True)
    events = [
        _event(1, {"type": "move_stop_loss", "target": "all", "value": "96"}, key="sl"),
        _event(2, {"type": "move_take_profit", "target": "tp2", "value": "112"}, key="tp"),
        _event(3, {"type": "move_to_break_even", "target": "all", "value": None}, key="be"),
        _event(4, {"type": "close_half", "target": "runner", "value": "104"}, key="half"),
        _event(5, {"type": "close", "target": "runner", "value": "106"}, key="runner"),
    ]
    bars = [_bar(0, high="104", low="99", close="102"), _bar(1, high="104", low="99", close="102"), _bar(2, high="104", low="99", close="102"), _bar(3, high="104", low="101", close="102"), _bar(4, high="104", low="101", close="102"), _bar(5, high="107", low="101", close="106")]
    state = _run(g, bars, events=events)
    assert state.effective_stop == Decimal("100")
    assert state.legs[1].effective_target == Decimal("112")
    runner = state.legs[2]
    assert runner.remaining_fraction == 0
    assert runner.realized_r == Decimal("1")


def test_11_terminal_bar_updates_mae_mfe_before_return() -> None:
    g = _geometry("BUY", targets=("105",))
    state = _run(g, [_bar(0, high="110", low="96", close="105")])
    assert state.terminal
    assert state.max_price == Decimal("110")
    assert state.min_price == Decimal("96")


def test_architecture_single_writer_immutable_geometry_dual_watermarks_and_cas() -> None:
    resolver = (ROOT / "app" / "aidy_shadow_resolver.py").read_text(encoding="utf-8")
    service = (ROOT / "app" / "shadow_trading_service_v4.py").read_text(encoding="utf-8")
    migration = (ROOT / "migrations" / "versions" / "0055_aidy_provider_lab_market_truth.py").read_text(encoding="utf-8")
    assert "aidy_original_geometry" in resolver
    assert "aidy_lifecycle_watermark" in resolver and "aidy_m1_cursor_at" in resolver
    assert "aidy_lifecycle_applied_count" in resolver
    assert "already_through = _utc(state.market_cursor)" not in resolver
    assert "aidy_state_version=:expected_version" in resolver
    assert "aidy_m1_cursor_at IS NOT DISTINCT FROM :expected_cursor" in resolver
    assert "aidy_lifecycle_watermark IS NOT DISTINCT FROM :expected_lifecycle_mark" in resolver
    assert "aidy_lifecycle_applied_count=:expected_lifecycle_count" in resolver
    assert "AND NOT COALESCE(aidy_terminal,false)" in resolver
    assert "origin='provider_update'" in resolver
    assert "if aidy_row is not None:\n            return True" in service
    assert "aidy_m1_revalidation_required" in migration
    assert "ck_shadow_aidy_score_mode" in migration
    assert "quote_mode<>'aidy_m1'" in migration


def test_architecture_original_truth_and_one_window_continuity() -> None:
    resolver = (ROOT / "app" / "aidy_shadow_resolver.py").read_text(encoding="utf-8")
    client = (ROOT / "app" / "aidy_market_client.py").read_text(encoding="utf-8")
    migration = (ROOT / "migrations" / "versions" / "0055_aidy_provider_lab_market_truth.py").read_text(encoding="utf-8")
    assert 'OriginalGeometry.from_payload(trade.get("aidy_original_geometry"))' in resolver
    assert 'row.get("aidy_original_target")' in resolver and "aidy_effective_target" in resolver
    assert "t.take_profits" in migration and "aidy_original_target" in migration
    assert '"expected_open_times"' in client and '"missing_open_times"' in client
    assert "calculated_missing != missing" in client
    assert "first_observed_after_pit_cutoff" in client
    assert "while cursor < end" not in client
