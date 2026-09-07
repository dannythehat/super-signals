from __future__ import annotations

from uuid import UUID

from app.provider_day12_fingerprint import (
    MINIMUM_FORWARD_N,
    PRIMARY_ESTIMATE_KIND,
    STATISTICAL_STATUS,
    FingerprintObservation,
    _binary_posterior,
    build_fingerprint_cells,
)


def _obs(i: int, *, source: int, side: str, session: str, tp1: bool) -> FingerprintObservation:
    return FingerprintObservation(
        trade_id=UUID(int=1000 + i),
        source_id=UUID(int=source),
        side=side,
        session_bucket=session,
        duration_seconds=600.0 + i,
        mae_r=0.2 + i * 0.01,
        mfe_r=0.7 + i * 0.02,
        target_hits=((1, tp1), (2, tp1 and i % 2 == 0)),
        stop_hit=not tp1,
        break_even=False,
    )


def test_day12_constants_are_fail_closed() -> None:
    assert MINIMUM_FORWARD_N == 30
    assert STATISTICAL_STATUS == "WAITING-FOR-FORWARD-EVIDENCE"
    assert PRIMARY_ESTIMATE_KIND == "posterior_partial_pool"


def test_partial_pooling_shrinks_sparse_extreme_more_than_dense_extreme() -> None:
    sparse = _binary_posterior(successes=1, n=1, population_successes=50, population_n=100)
    dense = _binary_posterior(successes=30, n=30, population_successes=50, population_n=100)
    assert 0.5 < sparse["posterior_mean"] < 1.0
    assert dense["posterior_mean"] > sparse["posterior_mean"]
    sparse_width = sparse["credible_upper_95"] - sparse["credible_lower_95"]
    dense_width = dense["credible_upper_95"] - dense["credible_lower_95"]
    assert sparse_width > dense_width


def test_fingerprint_is_deterministic_and_covers_direction_session_metrics() -> None:
    rows = [
        _obs(1, source=1, side="BUY", session="london", tp1=True),
        _obs(2, source=1, side="BUY", session="new_york", tp1=False),
        _obs(3, source=1, side="SELL", session="london", tp1=True),
        _obs(4, source=2, side="SELL", session="asia", tp1=False),
    ]
    first = build_fingerprint_cells(rows)
    second = build_fingerprint_cells(reversed(rows))
    assert first == second
    kinds = {row["dimension_kind"] for row in first}
    assert kinds == {"provider", "provider_direction", "provider_session", "provider_direction_session"}
    provider = next(row for row in first if row["source_id"] == UUID(int=1) and row["dimension_kind"] == "provider")
    assert provider["posterior"]["primary_estimate_kind"] == PRIMARY_ESTIMATE_KIND
    assert "tp_hit_rates" in provider["posterior"]
    assert "duration_seconds" in provider["posterior"]
    assert "mae_r" in provider["posterior"]
    assert "mfe_r" in provider["posterior"]
    assert provider["descriptive"]["raw_values_are_descriptive_only"] is True
    assert provider["statistical_status"] == STATISTICAL_STATUS
    assert provider["n_gate_met"] is False


def test_zero_evidence_creates_no_fake_cell() -> None:
    assert build_fingerprint_cells([]) == []
