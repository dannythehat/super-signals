"""Read-only per-provider coverage report: understood vs. acted on.

Every message the AI supervisor recognises as a genuine trade signal, update or close -
whether or not it could safely be executed - is already recorded in
`provider_trade_observations` (see `ai_message_pipeline.py::_store_observation`). That
table exists precisely so a provider whose management language is never acted on is not
invisible; this endpoint is the first thing that reads it back.

This surfaces, per source, how many observations in a recent window were understood but
not executed, and why - most often `unsupported_management` (a close/TP/BE instruction
that couldn't be mapped unambiguously to one open position) or `provider_result_only` (a
result narrated with no structured instruction attached). Neither is necessarily a bug:
declining to guess which trade an ambiguous message means is the safer choice. This
report exists so that judgement is made by a human looking at real volume per provider,
not left invisible until a stuck position surfaces it weeks later.

Read-only. Touches no execution, dispatch or classification state.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session

router = APIRouter(prefix="/admin/telegram/provider-coverage", tags=["provider coverage"])
DbSession = Annotated[Session, Depends(get_db_session)]
SourceManager = Annotated[
    dict[str, Any],
    Depends(require_permission("sources.manage")),
]


class ReasonCount(BaseModel):
    reason: str
    count: int


class ProviderCoverageRow(BaseModel):
    source_id: str
    source_title: str
    source_status: str
    window_days: int
    total_observations: int
    executed_count: int
    understood_not_executed_count: int
    top_reasons: list[ReasonCount]


@router.get("/summary", response_model=list[ProviderCoverageRow])
def provider_coverage_summary(
    response: Response,
    session: DbSession,
    identity: SourceManager,
    days: Annotated[int, Query(ge=1, le=30)] = 7,
    top_reasons_per_source: Annotated[int, Query(ge=1, le=10)] = 5,
) -> list[ProviderCoverageRow]:
    del identity
    totals = session.execute(
        text(
            """
            SELECT
                s.id AS source_id,
                COALESCE(s.source_alias, s.chat_title) AS source_title,
                s.status AS source_status,
                COUNT(*) FILTER (WHERE pto.observed_at > now() - make_interval(days => :days)) AS total_observations,
                COUNT(*) FILTER (
                    WHERE pto.observed_at > now() - make_interval(days => :days)
                      AND pto.executable
                ) AS executed_count,
                COUNT(*) FILTER (
                    WHERE pto.observed_at > now() - make_interval(days => :days)
                      AND NOT pto.executable
                ) AS not_executed_count
            FROM sources s
            LEFT JOIN provider_trade_observations pto ON pto.source_id = s.id
            WHERE s.status <> 'revoked'
            GROUP BY s.id, source_title, s.status
            HAVING COUNT(*) FILTER (WHERE pto.observed_at > now() - make_interval(days => :days)) > 0
            ORDER BY not_executed_count DESC, total_observations DESC
            """
        ),
        {"days": days},
    ).mappings().all()

    reasons_by_source: dict[str, list[ReasonCount]] = {}
    if totals:
        reason_rows = session.execute(
            text(
                """
                SELECT source_id, outcome_reason, COUNT(*) AS n
                FROM provider_trade_observations
                WHERE observed_at > now() - make_interval(days => :days)
                  AND NOT executable
                  AND source_id = ANY(:source_ids)
                GROUP BY source_id, outcome_reason
                ORDER BY source_id, n DESC
                """
            ),
            {"days": days, "source_ids": [row["source_id"] for row in totals]},
        ).mappings().all()
        for row in reason_rows:
            key = str(row["source_id"])
            bucket = reasons_by_source.setdefault(key, [])
            if len(bucket) < top_reasons_per_source:
                bucket.append(
                    ReasonCount(reason=str(row["outcome_reason"] or "unspecified"), count=int(row["n"]))
                )

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return [
        ProviderCoverageRow(
            source_id=str(row["source_id"]),
            source_title=str(row["source_title"] or ""),
            source_status=str(row["source_status"]),
            window_days=days,
            total_observations=int(row["total_observations"]),
            executed_count=int(row["executed_count"]),
            understood_not_executed_count=int(row["not_executed_count"]),
            top_reasons=reasons_by_source.get(str(row["source_id"]), []),
        )
        for row in totals
    ]
