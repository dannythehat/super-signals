"""Owner-only read model surfacing AIDY's Decision Ledger in one place.

Observability only: no writes, no broker calls, no OpenAI calls. Reads stored database
state exactly as it stands; never recomputes or refreshes anything just because a
browser loaded the page, matching the same rule the AIDY Data Hub concept was built
around. This exists because the alternative was the owner having no way to see AIDY's
decisions, outcomes or cohort evidence without someone running raw SQL for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

# A cohort cell below this many resolved trades is too thin to surface as a standout --
# same floor discipline as the rest of the Decision Ledger: a clean-looking number from
# 3 trades is noise, not evidence.
_MINIMUM_COHORT_SAMPLE = 15
_COHORT_STANDOUT_LIMIT = 5


@dataclass(frozen=True, slots=True)
class DecisionClassSummary:
    decision_class: str
    decision_count: int
    scored_count: int
    net_delta_usd: Decimal
    confirmed_helped: int
    confirmed_hurt: int
    neutral: int
    open_at_window_end: int


@dataclass(frozen=True, slots=True)
class CohortStandout:
    provider: str
    side: str
    session: str
    weekday: str
    trades_resolved: int
    win_rate_pct: Decimal | None
    net_pnl_usd: Decimal


@dataclass(frozen=True, slots=True)
class HypothesisRegistryStatus:
    preregistered_count: int
    tested_count: int
    significant_count: int
    run_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    total_sources: int
    sources_with_observations: int
    sources_with_decisions: int


@dataclass(frozen=True, slots=True)
class ProviderCoverage:
    provider: str
    decision_count: int
    trades_resolved: int
    win_rate_pct: Decimal | None
    net_pnl_usd: Decimal
    scored_coverage_pct: Decimal | None


@dataclass(frozen=True, slots=True)
class AidyOverviewView:
    generated_at: datetime
    total_decisions: int
    last_decision_at: datetime | None
    total_outcomes_scored: int
    last_outcome_at: datetime | None
    by_class: tuple[DecisionClassSummary, ...]
    top_cohorts: tuple[CohortStandout, ...]
    bottom_cohorts: tuple[CohortStandout, ...]
    hypothesis_registry: HypothesisRegistryStatus | None
    coverage: CoverageSummary
    providers: tuple[ProviderCoverage, ...]


class AidyOverviewService:
    """Read AIDY's decision, outcome and cohort state as it stands right now."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def read(self) -> AidyOverviewView:
        with self._session_factory() as session:
            decisions_row = session.execute(
                text("SELECT count(*) AS total, max(decided_at) AS last_at FROM aidy_decisions")
            ).mappings().one()
            outcomes_row = session.execute(
                text(
                    "SELECT count(*) AS total, max(resolved_at) AS last_at "
                    "FROM aidy_decision_outcomes"
                )
            ).mappings().one()
            by_class = tuple(
                DecisionClassSummary(
                    decision_class=row["decision_class"],
                    decision_count=row["decision_count"],
                    scored_count=row["scored_count"],
                    net_delta_usd=Decimal(str(row["net_delta_usd"])),
                    confirmed_helped=row["confirmed_helped"],
                    confirmed_hurt=row["confirmed_hurt"],
                    neutral=row["neutral"],
                    open_at_window_end=row["open_at_window_end"],
                )
                for row in session.execute(text(_BY_CLASS_SQL)).mappings().all()
            )
            top_cohorts = tuple(
                _cohort_standout(row)
                for row in session.execute(
                    text(_COHORT_STANDOUT_SQL.format(direction="DESC")),
                    {"minimum": _MINIMUM_COHORT_SAMPLE, "limit": _COHORT_STANDOUT_LIMIT},
                ).mappings().all()
            )
            bottom_cohorts = tuple(
                _cohort_standout(row)
                for row in session.execute(
                    text(_COHORT_STANDOUT_SQL.format(direction="ASC")),
                    {"minimum": _MINIMUM_COHORT_SAMPLE, "limit": _COHORT_STANDOUT_LIMIT},
                ).mappings().all()
            )
            registry_row = session.execute(text(_HYPOTHESIS_REGISTRY_SQL)).mappings().first()
            registry = (
                HypothesisRegistryStatus(
                    preregistered_count=registry_row["preregistered_hypothesis_count"],
                    tested_count=registry_row["tested_hypothesis_count"],
                    significant_count=registry_row["bh_rejected_count"],
                    run_completed_at=registry_row["completed_at"],
                )
                if registry_row is not None
                else None
            )
            coverage_row = session.execute(text(_COVERAGE_SQL)).mappings().one()
            coverage = CoverageSummary(
                total_sources=coverage_row["total_sources"],
                sources_with_observations=coverage_row["sources_with_observations"],
                sources_with_decisions=coverage_row["sources_with_decisions"],
            )
            providers = tuple(
                ProviderCoverage(
                    provider=row["provider"],
                    decision_count=row["decision_count"],
                    trades_resolved=row["trades_resolved"],
                    win_rate_pct=(
                        Decimal(str(row["win_rate_pct"]))
                        if row["win_rate_pct"] is not None
                        else None
                    ),
                    net_pnl_usd=Decimal(str(row["net_pnl_usd"])),
                    scored_coverage_pct=(
                        Decimal(str(row["scored_coverage_pct"]))
                        if row["scored_coverage_pct"] is not None
                        else None
                    ),
                )
                for row in session.execute(text(_PROVIDER_COVERAGE_SQL)).mappings().all()
            )

        return AidyOverviewView(
            generated_at=_utcnow(),
            total_decisions=decisions_row["total"],
            last_decision_at=decisions_row["last_at"],
            total_outcomes_scored=outcomes_row["total"],
            last_outcome_at=outcomes_row["last_at"],
            by_class=by_class,
            top_cohorts=top_cohorts,
            bottom_cohorts=bottom_cohorts,
            hypothesis_registry=registry,
            coverage=coverage,
            providers=providers,
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _cohort_standout(row: dict) -> CohortStandout:
    return CohortStandout(
        provider=row["provider"],
        side=row["side"],
        session=row["session"],
        weekday=row["weekday"],
        trades_resolved=row["trades_resolved"],
        win_rate_pct=(
            Decimal(str(row["win_rate_pct"])) if row["win_rate_pct"] is not None else None
        ),
        net_pnl_usd=Decimal(str(row["net_pnl_usd"])),
    )


_BY_CLASS_SQL = """
    SELECT
        d.decision_class,
        count(*) AS decision_count,
        count(o.id) AS scored_count,
        COALESCE(sum(o.decision_delta_usd), 0) AS net_delta_usd,
        count(*) FILTER (WHERE o.resolution = 'confirmed_helped') AS confirmed_helped,
        count(*) FILTER (WHERE o.resolution = 'confirmed_hurt') AS confirmed_hurt,
        count(*) FILTER (WHERE o.resolution = 'neutral') AS neutral,
        count(*) FILTER (WHERE o.resolution = 'open_at_window_end') AS open_at_window_end
    FROM aidy_decisions d
    LEFT JOIN aidy_decision_outcomes o ON o.decision_id = d.id
    GROUP BY d.decision_class
    ORDER BY d.decision_class
"""

_COHORT_STANDOUT_SQL = """
    SELECT provider, side, session, weekday, trades_resolved, win_rate_pct, net_pnl_usd
    FROM provider_trade_scoreboard_by_cohort
    WHERE trades_resolved >= :minimum
    ORDER BY net_pnl_usd {direction}
    LIMIT :limit
"""

_HYPOTHESIS_REGISTRY_SQL = """
    SELECT preregistered_hypothesis_count, tested_hypothesis_count, bh_rejected_count, completed_at
    FROM provider_conditional_runs
    WHERE completed_at IS NOT NULL
    ORDER BY completed_at DESC, id DESC
    LIMIT 1
"""

# Independent subqueries, not one big join -- a single query joining sources against
# provider_trade_observations directly times out at this row count (36k+ observations).
_COVERAGE_SQL = """
    SELECT
        (SELECT count(*) FROM sources) AS total_sources,
        (SELECT count(DISTINCT source_id) FROM provider_trade_observations)
            AS sources_with_observations,
        (SELECT count(DISTINCT source_id) FROM aidy_decisions) AS sources_with_decisions
"""

# Every provider AIDY has ever decided on, blended (not sliced by cohort) so all of them
# show up here even though most never clear the cohort view's 15-per-slice floor -- this
# is what answers "is AIDY actually studying everyone," not just the standouts.
_PROVIDER_COVERAGE_SQL = """
    SELECT
        COALESCE(NULLIF(s.chat_title, ''), s.source_alias) AS provider,
        count(d.id) AS decision_count,
        COALESCE(bd.trades_resolved, 0) AS trades_resolved,
        bd.win_rate_pct,
        COALESCE(bd.net_pnl_usd, 0) AS net_pnl_usd,
        bd.scored_coverage_pct
    FROM sources s
    JOIN aidy_decisions d ON d.source_id = s.id
    LEFT JOIN provider_trade_scoreboard bd ON bd.source_id = s.id
    GROUP BY s.id, s.chat_title, s.source_alias, bd.trades_resolved, bd.win_rate_pct,
             bd.net_pnl_usd, bd.scored_coverage_pct
    ORDER BY COALESCE(bd.trades_resolved, 0) DESC, decision_count DESC
"""


__all__ = [
    "AidyOverviewService",
    "AidyOverviewView",
    "CohortStandout",
    "CoverageSummary",
    "DecisionClassSummary",
    "HypothesisRegistryStatus",
    "ProviderCoverage",
]
