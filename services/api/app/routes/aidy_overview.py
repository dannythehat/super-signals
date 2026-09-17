"""Owner-only read endpoint for AIDY's Decision Ledger, outcomes and cohort evidence."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from app.access_control import require_permission
from app.aidy_overview import AidyOverviewService
from app.db import get_session_factory

router = APIRouter(prefix="/admin/aidy", tags=["admin-aidy"])
ActivityAdmin = Annotated[dict[str, Any], Depends(require_permission("activity.view"))]


class DecisionClassSummaryResponse(BaseModel):
    decision_class: str
    decision_count: int
    scored_count: int
    net_delta_usd: Decimal
    confirmed_helped: int
    confirmed_hurt: int
    neutral: int
    open_at_window_end: int


class CohortStandoutResponse(BaseModel):
    provider: str
    side: str
    session: str
    weekday: str
    trades_resolved: int
    win_rate_pct: Decimal | None
    net_pnl_usd: Decimal


class HypothesisRegistryResponse(BaseModel):
    preregistered_count: int
    tested_count: int
    significant_count: int
    run_completed_at: datetime | None


class AidyOverviewResponse(BaseModel):
    generated_at: datetime
    total_decisions: int
    last_decision_at: datetime | None
    total_outcomes_scored: int
    last_outcome_at: datetime | None
    by_class: tuple[DecisionClassSummaryResponse, ...]
    top_cohorts: tuple[CohortStandoutResponse, ...]
    bottom_cohorts: tuple[CohortStandoutResponse, ...]
    hypothesis_registry: HypothesisRegistryResponse | None


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.get("/overview", response_model=AidyOverviewResponse)
async def aidy_overview(
    request: Request,
    response: Response,
    _actor: ActivityAdmin,
) -> AidyOverviewResponse:
    service = AidyOverviewService(get_session_factory())
    view = service.read()
    _no_store(response)
    return AidyOverviewResponse(
        generated_at=view.generated_at,
        total_decisions=view.total_decisions,
        last_decision_at=view.last_decision_at,
        total_outcomes_scored=view.total_outcomes_scored,
        last_outcome_at=view.last_outcome_at,
        by_class=tuple(
            DecisionClassSummaryResponse(**asdict(item)) for item in view.by_class
        ),
        top_cohorts=tuple(
            CohortStandoutResponse(**asdict(item)) for item in view.top_cohorts
        ),
        bottom_cohorts=tuple(
            CohortStandoutResponse(**asdict(item)) for item in view.bottom_cohorts
        ),
        hypothesis_registry=(
            HypothesisRegistryResponse(**asdict(view.hypothesis_registry))
            if view.hypothesis_registry is not None
            else None
        ),
    )


__all__ = ["router"]
