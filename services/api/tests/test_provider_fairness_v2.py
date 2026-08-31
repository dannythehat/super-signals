from datetime import UTC, datetime
from decimal import Decimal

from app.provider_fairness import (
    BENCHMARK_MODEL,
    BENCHMARK_RISK_PER_LEG_USD,
    BENCHMARK_START_BALANCE_USD,
    evidence_minimums,
    score_eligibility,
    session_bucket,
)
from app.shadow_trading_v2 import ShadowTradeService, _benchmark_pnl_usd, _leg_r


def test_fixed_provider_benchmark_never_compounds_or_weights_tp_numbers():
    assert BENCHMARK_MODEL == "fixed_1000_10_per_tp_fair_v2"
    assert BENCHMARK_START_BALANCE_USD == Decimal("1000")
    assert BENCHMARK_RISK_PER_LEG_USD == Decimal("10")
    assert _benchmark_pnl_usd(Decimal("1")) == Decimal("10.00")
    assert _benchmark_pnl_usd(Decimal("-1")) == Decimal("-10.00")


def test_r_multiple_is_normalized_by_each_provider_stop_distance():
    assert _leg_r(
        entry=Decimal("4500"),
        exit_price=Decimal("4510"),
        initial_stop=Decimal("4490"),
        side="BUY",
    ) == Decimal("1")
    assert _leg_r(
        entry=Decimal("4500"),
        exit_price=Decimal("4480"),
        initial_stop=Decimal("4510"),
        side="SELL",
    ) == Decimal("2")


def test_scalpers_require_tick_resolution_but_slower_styles_accept_quotes():
    assert score_eligibility(style="scalper", quote_mode="stream_tick") == (True, None)
    assert score_eligibility(style="scalper", quote_mode="stream_quote") == (
        False,
        "scalper_requires_tick_resolution",
    )
    assert score_eligibility(style="scalper", quote_mode="snapshot_poll") == (
        False,
        "scalper_requires_tick_resolution",
    )
    assert score_eligibility(style="intraday", quote_mode="stream_quote") == (True, None)
    assert score_eligibility(style="swing_or_sparse", quote_mode="snapshot_poll") == (True, None)


def test_style_evidence_thresholds_are_deliberately_different():
    assert evidence_minimums("scalper") == (100, 20, 4)
    assert evidence_minimums("intraday") == (60, 30, 6)
    assert evidence_minimums("swing_or_sparse") == (30, 45, 8)


def test_gold_session_buckets_are_stable_in_utc():
    assert session_bucket(datetime(2026, 8, 31, 1, tzinfo=UTC)) == "asia"
    assert session_bucket(datetime(2026, 8, 31, 9, tzinfo=UTC)) == "london"
    assert session_bucket(datetime(2026, 8, 31, 13, tzinfo=UTC)) == "london_new_york_overlap"
    assert session_bucket(datetime(2026, 8, 31, 18, tzinfo=UTC)) == "new_york"
    assert session_bucket(datetime(2026, 8, 31, 21, 30, tzinfo=UTC)) == "rollover"


def test_layer_parser_turns_explicit_second_entry_into_separate_actions():
    row = {
        "entry_low": Decimal("4490"),
        "entry_high": Decimal("4500"),
        "side": "BUY",
        "order_type": "market",
        "original_text": "BUY GOLD\nENTRY 1: 4500\nENTRY 2: 4490\nSL 4480\nTP1 4510",
    }
    entries = ShadowTradeService._research_entries(row)
    assert [(item.entry_index, item.order_type, item.low) for item in entries] == [
        (1, "market", Decimal("4500")),
        (2, "buy_limit", Decimal("4490")),
    ]


def test_plain_entry_zone_remains_one_action_not_an_invented_grid():
    row = {
        "entry_low": Decimal("4495"),
        "entry_high": Decimal("4500"),
        "side": "BUY",
        "order_type": "market",
        "original_text": "BUY GOLD 4495-4500\nSL 4485\nTP1 4510",
    }
    entries = ShadowTradeService._research_entries(row)
    assert len(entries) == 1
    assert entries[0].entry_index == 1
    assert entries[0].order_type == "zone"
    assert entries[0].low == Decimal("4495")
    assert entries[0].high == Decimal("4500")
