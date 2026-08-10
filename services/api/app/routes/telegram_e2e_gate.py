"""Owner-only Day 21 Telegram end-to-end acceptance gate."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session
from app.telegram_e2e_gate import evaluate_telegram_e2e_gate

router = APIRouter(prefix="/admin/telegram-e2e-gate", tags=["telegram-e2e-gate"])
DbSession = Annotated[Session, Depends(get_db_session)]
SourceManager = Annotated[dict[str, Any], Depends(require_permission("sources.manage"))]


class GateCheckResponse(BaseModel):
    key: str
    status: str
    observed: int
    expected: str
    detail: str


class GateReportResponse(BaseModel):
    source_id: UUID
    source_alias: str
    source_status: str
    status: str
    generated_at: str
    checks: list[GateCheckResponse]


@router.get("", response_model=GateReportResponse)
def telegram_e2e_gate(
    response: Response,
    session: DbSession,
    identity: SourceManager,
    source_id: Annotated[UUID, Query(description="Logical Telegram source to verify")],
) -> GateReportResponse:
    del identity
    try:
        report = evaluate_telegram_e2e_gate(session, source_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    payload = asdict(report)
    payload["generated_at"] = report.generated_at.isoformat()
    payload["checks"] = [asdict(check) for check in report.checks]
    return GateReportResponse(**payload)
