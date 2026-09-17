"""Compute every provider's fingerprint and persist it -- append-only, one row per pass.

Kept as a full history rather than an upsert: whether a provider's fingerprint is stable or
drifting over time is itself real signal, and overwriting the last computation would throw
that away. aidy_reasoning_engine.py always reads the latest row per provider.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.provider_fingerprint_engine import ProviderFingerprint, ProviderFingerprintEngine

logger = logging.getLogger(__name__)

_INSERT = """
    INSERT INTO provider_trade_fingerprints (
        id, source_id, trading_style, trades_resolved, wins, losses, win_rate_pct,
        avg_stop_distance_won, avg_stop_distance_lost, avg_planned_rr_won, avg_planned_rr_lost,
        geometry_sample_met, best_side, best_side_win_rate_pct, best_side_trades,
        worst_side, worst_side_win_rate_pct, worst_side_trades,
        best_session, best_session_win_rate_pct, best_session_trades,
        worst_session, worst_session_win_rate_pct, worst_session_trades,
        cohort_sample_met, summary
    ) VALUES (
        :id, :source_id, :trading_style, :trades_resolved, :wins, :losses, :win_rate_pct,
        :avg_stop_distance_won, :avg_stop_distance_lost, :avg_planned_rr_won, :avg_planned_rr_lost,
        :geometry_sample_met, :best_side, :best_side_win_rate_pct, :best_side_trades,
        :worst_side, :worst_side_win_rate_pct, :worst_side_trades,
        :best_session, :best_session_win_rate_pct, :best_session_trades,
        :worst_session, :worst_session_win_rate_pct, :worst_session_trades,
        :cohort_sample_met, :summary
    )
"""


def _row(fp: ProviderFingerprint) -> dict:
    return {
        "id": uuid4(),
        "source_id": fp.source_id,
        "trading_style": fp.trading_style,
        "trades_resolved": fp.trades_resolved,
        "wins": fp.wins,
        "losses": fp.losses,
        "win_rate_pct": fp.win_rate_pct,
        "avg_stop_distance_won": fp.avg_stop_distance_won,
        "avg_stop_distance_lost": fp.avg_stop_distance_lost,
        "avg_planned_rr_won": fp.avg_planned_rr_won,
        "avg_planned_rr_lost": fp.avg_planned_rr_lost,
        "geometry_sample_met": fp.geometry_sample_met,
        "best_side": fp.best_side,
        "best_side_win_rate_pct": fp.best_side_win_rate_pct,
        "best_side_trades": fp.best_side_trades,
        "worst_side": fp.worst_side,
        "worst_side_win_rate_pct": fp.worst_side_win_rate_pct,
        "worst_side_trades": fp.worst_side_trades,
        "best_session": fp.best_session,
        "best_session_win_rate_pct": fp.best_session_win_rate_pct,
        "best_session_trades": fp.best_session_trades,
        "worst_session": fp.worst_session,
        "worst_session_win_rate_pct": fp.worst_session_win_rate_pct,
        "worst_session_trades": fp.worst_session_trades,
        "cohort_sample_met": fp.cohort_sample_met,
        "summary": fp.summary,
    }


@dataclass
class FingerprintSummary:
    computed: int = 0

    def as_text(self) -> str:
        return f"computed={self.computed}"


class ProviderFingerprintRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        engine: ProviderFingerprintEngine | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine or ProviderFingerprintEngine(session_factory)

    def _persist(self, fingerprints: list[ProviderFingerprint]) -> None:
        if not fingerprints:
            return
        with self._session_factory() as session:
            session.execute(text(_INSERT), [_row(fp) for fp in fingerprints])
            session.commit()

    async def run(self, *, minimum_total: int = 5) -> FingerprintSummary:
        fingerprints = await asyncio.to_thread(
            self._engine.compute_all, minimum_total=minimum_total
        )
        await asyncio.to_thread(self._persist, fingerprints)
        summary = FingerprintSummary(computed=len(fingerprints))
        logger.info("Provider fingerprint pass computed=%s", summary.computed)
        return summary


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum-total", type=int, default=5)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", flush=True)
        return 2

    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        runner = ProviderFingerprintRunner(sessionmaker(bind=engine, future=True))
        summary = await runner.run(minimum_total=args.minimum_total)
    finally:
        engine.dispose()
    print(summary.as_text(), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(asyncio.run(_main()))


__all__ = ["FingerprintSummary", "ProviderFingerprintRunner"]
