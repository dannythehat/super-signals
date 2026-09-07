from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.layer_allocation import allocate_entry_targets

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class _Entry:
    entry_index: int


def test_shared_allocation_matches_canonical_two_entry_plan() -> None:
    entries = (_Entry(1), _Entry(2))
    targets = (Decimal("1"), Decimal("2"), Decimal("3"), None)
    plan = allocate_entry_targets(entries, targets)
    assert [(x.entry.entry_index, x.tp_index) for x in plan] == [
        (1, 1),
        (2, 2),
        (1, 3),
        (2, 4),
    ]


def test_live_executors_delegate_to_shared_allocation_helper() -> None:
    for filename in ("paper_critical_execution.py", "trading_execution_canonical.py"):
        source = (ROOT / "app" / filename).read_text(encoding="utf-8")
        assert "from app.layer_allocation import allocate_entry_targets" in source
        assert source.count("def _allocation_pairs(") == 1
        method = source.split("def _allocation_pairs(", 1)[1]
        assert "allocate_entry_targets(entries, targets)" in method


def test_calibration_replay_is_leg_keyed_and_independent() -> None:
    source = (ROOT / "app" / "provider_day11_calibration_replay.py").read_text(
        encoding="utf-8"
    )
    assert "allocate_entry_targets(entries, tuple(all_targets))" in source
    assert "_broker_leg_truth" in source
    assert "paper_broker_leg_key_mismatch" in source
    assert "abs_r_delta = max(leg_deltas.values()" in source
    assert "uuid5" in source
    assert "uuid4" not in source
    paper_done = source.index("paper_legs: dict[tuple[int, int], Decimal]")
    broker_read = source.index(
        "broker_legs, broker_lifecycle, broker_deals, broker_error = _broker_leg_truth("
    )
    assert paper_done < broker_read


def test_broker_truth_uses_final_position_stop_for_lifecycle_only() -> None:
    source = (ROOT / "app" / "provider_day11_replay.py").read_text(encoding="utf-8")
    broker = source.split("def _broker_leg_truth(", 1)[1].split("def _broker_truth(", 1)[0]
    assert "p.stop_loss" in broker
    assert 'final_stop = _decimal(row["stop_loss"])' in broker
    assert "abs(exit_price - final_stop)" in broker
    assert "risk = abs(entry - initial_stop)" in broker


def test_calibration_retry_is_bounded_to_500_503() -> None:
    source = (ROOT / "app" / "aidy_market_client.py").read_text(encoding="utf-8")
    calibration = source.split("async def fetch_calibration_m1", 1)[1]
    assert "_CALIBRATION_RETRY_ATTEMPTS = 3" in source
    assert "response.status_code in {500, 503}" in calibration
    assert "2 ** attempt" in calibration
    assert "while True" not in calibration
