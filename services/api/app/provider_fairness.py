"""Deterministic Provider Lab fairness policy.

These helpers define only research benchmarking rules. They never place orders or alter
live provider allocations. Every research provider is measured on the same fixed $1,000
account and $10 cash risk per TP leg, while execution-resolution and evidence gates adapt
to the provider's observed trading style.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

BENCHMARK_MODEL = "fixed_1000_10_per_tp_fair_v2"
BENCHMARK_START_BALANCE_USD = Decimal("1000")
BENCHMARK_RISK_PER_LEG_USD = Decimal("10")


STYLE_MINIMUM_EVIDENCE: dict[str, tuple[int, int, int]] = {
    # closed trades, distinct trading days, distinct calendar weeks
    "scalper": (100, 20, 4),
    "intraday": (60, 30, 6),
    "swing_or_sparse": (30, 45, 8),
    "mixed": (60, 30, 6),
    "unknown": (60, 30, 6),
}


def session_bucket(posted_at: datetime) -> str:
    """Return one stable UTC Gold-session bucket for apples-to-apples segmentation."""
    hour = posted_at.hour
    if hour >= 22 or hour < 7:
        return "asia"
    if hour < 12:
        return "london"
    if hour < 16:
        return "london_new_york_overlap"
    if hour < 21:
        return "new_york"
    return "rollover"


def required_resolution(style: str) -> str:
    """Scalpers require tick-quality entry/outcome evidence; others accept quote data."""
    return "tick" if (style or "unknown").strip().lower() == "scalper" else "quote"


def score_eligibility(*, style: str, quote_mode: str) -> tuple[bool, str | None]:
    """Decide whether one shadow trade has sufficient market-data resolution to score."""
    required = required_resolution(style)
    mode = (quote_mode or "").strip().lower()
    if required == "tick" and mode != "stream_tick":
        return False, "scalper_requires_tick_resolution"
    if mode not in {"stream_tick", "stream_quote", "snapshot_poll"}:
        return False, "market_data_resolution_unknown"
    return True, None


def evidence_minimums(style: str) -> tuple[int, int, int]:
    return STYLE_MINIMUM_EVIDENCE.get((style or "unknown").strip().lower(), STYLE_MINIMUM_EVIDENCE["unknown"])


__all__ = [
    "BENCHMARK_MODEL",
    "BENCHMARK_RISK_PER_LEG_USD",
    "BENCHMARK_START_BALANCE_USD",
    "STYLE_MINIMUM_EVIDENCE",
    "evidence_minimums",
    "required_resolution",
    "score_eligibility",
    "session_bucket",
]
