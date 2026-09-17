"""Compute what actually separates each provider's wins from its losses -- and its style.

Deliberately descriptive, not statistically certified: this reads trades that have already
resolved and compares winners against losers from the same provider, so it never needs to wait
on new forward evidence the way the Day 13 conditional-hypothesis registry does. That is also
its limit -- a "best session" or "wins have tighter stops" finding here is a real pattern in
what already happened, not a proven causal rule, and never claims to be. Day 13 remains the
only place a statistically-certified claim can come from.

The point of computing this is that it gets used: aidy_reasoning_engine.py reads a provider's
latest fingerprint summary into its prompt, so a new signal is judged against how *this
provider's* own wins and losses actually look, not generic geometry rules alone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

MODEL_VERSION = "provider_fingerprint_v1"

# Need both outcomes represented with enough N each to compare geometry honestly -- fewer
# than this and "winners had bigger stops" is one lucky/unlucky trade, not a pattern.
_MINIMUM_GEOMETRY_SAMPLE_PER_OUTCOME = 8
# Matches the existing cohort-standout floor elsewhere in the Decision Ledger (aidy_overview.py)
# -- a cell below this many resolved trades is too thin to call a provider's best/worst.
_MINIMUM_COHORT_SAMPLE = 15


@dataclass(frozen=True, slots=True)
class ProviderFingerprint:
    source_id: UUID
    provider_name: str
    trading_style: str | None
    trades_resolved: int
    wins: int
    losses: int
    win_rate_pct: Decimal | None
    avg_stop_distance_won: Decimal | None
    avg_stop_distance_lost: Decimal | None
    avg_planned_rr_won: Decimal | None
    avg_planned_rr_lost: Decimal | None
    geometry_sample_met: bool
    best_side: str | None
    best_side_win_rate_pct: Decimal | None
    best_side_trades: int | None
    worst_side: str | None
    worst_side_win_rate_pct: Decimal | None
    worst_side_trades: int | None
    best_session: str | None
    best_session_win_rate_pct: Decimal | None
    best_session_trades: int | None
    worst_session: str | None
    worst_session_win_rate_pct: Decimal | None
    worst_session_trades: int | None
    cohort_sample_met: bool
    summary: str


_BASE_SQL = """
    SELECT s.id AS source_id, COALESCE(NULLIF(s.chat_title, ''), s.source_alias) AS provider_name,
           prp.style AS trading_style,
           COALESCE(b.trades_resolved, 0) AS trades_resolved,
           COALESCE(b.win_rate_pct, NULL) AS win_rate_pct
    FROM sources s
    LEFT JOIN provider_research_profiles prp ON prp.source_id = s.id
    LEFT JOIN provider_trade_scoreboard b ON b.source_id = s.id
    WHERE COALESCE(b.trades_resolved, 0) >= :minimum_total
    ORDER BY s.id
"""

_GEOMETRY_SQL = """
    SELECT
        ts.outcome,
        count(*) AS n,
        avg(ABS(o.entry_low - o.stop_loss)) AS avg_stop_distance,
        avg(ABS((o.take_profits->>0)::numeric - o.entry_low)
            / NULLIF(ABS(o.entry_low - o.stop_loss), 0)) AS avg_planned_rr
    FROM provider_trade_observations o
    JOIN provider_trade_scores ts ON ts.observation_id = o.id
    WHERE o.source_id = :source_id
      AND ts.outcome IN ('won', 'lost')
      AND o.entry_low IS NOT NULL AND o.stop_loss IS NOT NULL
      AND jsonb_array_length(o.take_profits) > 0
      AND o.entry_low != o.stop_loss
    GROUP BY ts.outcome
"""

_SIDE_COHORT_SQL = """
    SELECT side, sum(trades_resolved) AS trades,
           round(100.0 * sum(win_rate_pct * trades_resolved / 100.0)
               / NULLIF(sum(trades_resolved), 0), 1) AS win_rate_pct
    FROM provider_trade_scoreboard_by_cohort
    WHERE source_id = :source_id
    GROUP BY side
    HAVING sum(trades_resolved) >= :minimum
    ORDER BY win_rate_pct DESC
"""

_SESSION_COHORT_SQL = """
    SELECT session, sum(trades_resolved) AS trades,
           round(100.0 * sum(win_rate_pct * trades_resolved / 100.0)
               / NULLIF(sum(trades_resolved), 0), 1) AS win_rate_pct
    FROM provider_trade_scoreboard_by_cohort
    WHERE source_id = :source_id
    GROUP BY session
    HAVING sum(trades_resolved) >= :minimum
    ORDER BY win_rate_pct DESC
"""


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _build_summary(
    *,
    provider_name: str,
    trading_style: str | None,
    trades_resolved: int,
    win_rate_pct: Decimal | None,
    avg_stop_won: Decimal | None,
    avg_stop_lost: Decimal | None,
    avg_rr_won: Decimal | None,
    avg_rr_lost: Decimal | None,
    geometry_sample_met: bool,
    best_side: str | None,
    best_side_wr: Decimal | None,
    worst_side: str | None,
    worst_side_wr: Decimal | None,
    best_session: str | None,
    best_session_wr: Decimal | None,
    worst_session: str | None,
    worst_session_wr: Decimal | None,
    cohort_sample_met: bool,
) -> str:
    parts: list[str] = []
    style_text = (
        trading_style.replace("_", " ") if trading_style and trading_style != "unknown" else None
    )
    win_rate_text = f"{win_rate_pct}%" if win_rate_pct is not None else "unknown"
    if style_text:
        parts.append(f"{provider_name} trades as a {style_text}.")
        parts.append(f"{trades_resolved} resolved trades, {win_rate_text} win rate.")
    else:
        parts.append(
            f"{provider_name}: {trades_resolved} resolved trades, {win_rate_text} win rate."
        )
    if geometry_sample_met:
        if avg_stop_won is not None and avg_stop_lost is not None:
            wider = "wider" if avg_stop_won > avg_stop_lost else "tighter"
            parts.append(
                f"Winning trades run a {wider} stop on average (won {avg_stop_won:.2f} vs "
                f"lost {avg_stop_lost:.2f})."
            )
        if avg_rr_won is not None and avg_rr_lost is not None:
            parts.append(
                f"Planned reward:risk averages {avg_rr_won:.2f} on winners vs "
                f"{avg_rr_lost:.2f} on losers."
            )
    else:
        parts.append("Not enough resolved wins and losses yet to compare geometry honestly.")
    if cohort_sample_met and best_side and worst_side:
        parts.append(
            f"Strongest side is {best_side} ({best_side_wr}% win rate), weakest is {worst_side} "
            f"({worst_side_wr}% win rate)."
        )
    if cohort_sample_met and best_session and worst_session:
        parts.append(
            f"Strongest session is {best_session} ({best_session_wr}% win rate), weakest is "
            f"{worst_session} ({worst_session_wr}% win rate)."
        )
    if not cohort_sample_met:
        parts.append(
            "Not enough resolved trades in any single side/session slice yet to call "
            "a best or worst."
        )
    parts.append("Descriptive pattern from history so far -- not a statistically certified rule.")
    return " ".join(parts)


class ProviderFingerprintEngine:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def compute_all(self, *, minimum_total: int = 5) -> list[ProviderFingerprint]:
        with self._session_factory() as session:
            bases = (
                session.execute(text(_BASE_SQL), {"minimum_total": minimum_total}).mappings().all()
            )
            fingerprints: list[ProviderFingerprint] = []
            for base in bases:
                fingerprints.append(self._compute_one(session, base))
        return fingerprints

    def _compute_one(self, session: Session, base: Any) -> ProviderFingerprint:
        source_id = UUID(str(base["source_id"]))
        geometry_rows = {
            row["outcome"]: row
            for row in session.execute(text(_GEOMETRY_SQL), {"source_id": source_id})
            .mappings()
            .all()
        }
        won = geometry_rows.get("won")
        lost = geometry_rows.get("lost")
        geometry_sample_met = bool(
            won
            and lost
            and won["n"] >= _MINIMUM_GEOMETRY_SAMPLE_PER_OUTCOME
            and lost["n"] >= _MINIMUM_GEOMETRY_SAMPLE_PER_OUTCOME
        )
        avg_stop_won = (
            Decimal(str(won["avg_stop_distance"]))
            if geometry_sample_met and won["avg_stop_distance"] is not None
            else None
        )
        avg_stop_lost = (
            Decimal(str(lost["avg_stop_distance"]))
            if geometry_sample_met and lost["avg_stop_distance"] is not None
            else None
        )
        avg_rr_won = (
            Decimal(str(won["avg_planned_rr"]))
            if geometry_sample_met and won["avg_planned_rr"] is not None
            else None
        )
        avg_rr_lost = (
            Decimal(str(lost["avg_planned_rr"]))
            if geometry_sample_met and lost["avg_planned_rr"] is not None
            else None
        )

        sides = (
            session.execute(
                text(_SIDE_COHORT_SQL), {"source_id": source_id, "minimum": _MINIMUM_COHORT_SAMPLE}
            )
            .mappings()
            .all()
        )
        sessions_ = (
            session.execute(
                text(_SESSION_COHORT_SQL),
                {"source_id": source_id, "minimum": _MINIMUM_COHORT_SAMPLE},
            )
            .mappings()
            .all()
        )
        cohort_sample_met = (
            bool(sides) and bool(sessions_) and len(sides) >= 1 and len(sessions_) >= 1
        )

        best_side = sides[0] if sides else None
        worst_side = sides[-1] if sides else None
        best_session = sessions_[0] if sessions_ else None
        worst_session = sessions_[-1] if sessions_ else None
        # Comparing a side/session against itself is not a "best vs worst" finding.
        cohort_sample_met = bool(
            best_side and worst_side and best_side["side"] != worst_side["side"]
        ) and bool(
            best_session and worst_session and best_session["session"] != worst_session["session"]
        )

        provider_name = str(base["provider_name"])
        trading_style = str(base["trading_style"]) if base["trading_style"] else None
        win_rate_pct = (
            Decimal(str(base["win_rate_pct"])) if base["win_rate_pct"] is not None else None
        )

        summary = _build_summary(
            provider_name=provider_name,
            trading_style=trading_style,
            trades_resolved=int(base["trades_resolved"]),
            win_rate_pct=win_rate_pct,
            avg_stop_won=avg_stop_won,
            avg_stop_lost=avg_stop_lost,
            avg_rr_won=avg_rr_won,
            avg_rr_lost=avg_rr_lost,
            geometry_sample_met=geometry_sample_met,
            best_side=str(best_side["side"]) if cohort_sample_met and best_side else None,
            best_side_wr=Decimal(str(best_side["win_rate_pct"]))
            if cohort_sample_met and best_side
            else None,
            worst_side=str(worst_side["side"]) if cohort_sample_met and worst_side else None,
            worst_side_wr=Decimal(str(worst_side["win_rate_pct"]))
            if cohort_sample_met and worst_side
            else None,
            best_session=str(best_session["session"])
            if cohort_sample_met and best_session
            else None,
            best_session_wr=Decimal(str(best_session["win_rate_pct"]))
            if cohort_sample_met and best_session
            else None,
            worst_session=str(worst_session["session"])
            if cohort_sample_met and worst_session
            else None,
            worst_session_wr=Decimal(str(worst_session["win_rate_pct"]))
            if cohort_sample_met and worst_session
            else None,
            cohort_sample_met=cohort_sample_met,
        )

        return ProviderFingerprint(
            source_id=source_id,
            provider_name=provider_name,
            trading_style=trading_style,
            trades_resolved=int(base["trades_resolved"]),
            wins=int(won["n"]) if won else 0,
            losses=int(lost["n"]) if lost else 0,
            win_rate_pct=win_rate_pct,
            avg_stop_distance_won=avg_stop_won,
            avg_stop_distance_lost=avg_stop_lost,
            avg_planned_rr_won=avg_rr_won,
            avg_planned_rr_lost=avg_rr_lost,
            geometry_sample_met=geometry_sample_met,
            best_side=str(best_side["side"]) if cohort_sample_met and best_side else None,
            best_side_win_rate_pct=Decimal(str(best_side["win_rate_pct"]))
            if cohort_sample_met and best_side
            else None,
            best_side_trades=int(best_side["trades"]) if cohort_sample_met and best_side else None,
            worst_side=str(worst_side["side"]) if cohort_sample_met and worst_side else None,
            worst_side_win_rate_pct=Decimal(str(worst_side["win_rate_pct"]))
            if cohort_sample_met and worst_side
            else None,
            worst_side_trades=int(worst_side["trades"])
            if cohort_sample_met and worst_side
            else None,
            best_session=str(best_session["session"])
            if cohort_sample_met and best_session
            else None,
            best_session_win_rate_pct=Decimal(str(best_session["win_rate_pct"]))
            if cohort_sample_met and best_session
            else None,
            best_session_trades=int(best_session["trades"])
            if cohort_sample_met and best_session
            else None,
            worst_session=str(worst_session["session"])
            if cohort_sample_met and worst_session
            else None,
            worst_session_win_rate_pct=Decimal(str(worst_session["win_rate_pct"]))
            if cohort_sample_met and worst_session
            else None,
            worst_session_trades=int(worst_session["trades"])
            if cohort_sample_met and worst_session
            else None,
            cohort_sample_met=cohort_sample_met,
            summary=summary,
        )


__all__ = ["MODEL_VERSION", "ProviderFingerprint", "ProviderFingerprintEngine"]
