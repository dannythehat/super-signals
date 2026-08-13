"""Day 36 member-facing manual MT5 reconciliation endpoint."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.access_control import get_current_identity
from app.manual_reconciliation_day36 import (
    Day36ManualMt5ReconciliationService,
    Day36ReconciliationError,
)
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service

router = APIRouter(tags=["mt5-day36"])
Identity = Annotated[dict[str, Any], Depends(get_current_identity)]


class ManualActionResponse(BaseModel):
    audit_id: UUID
    position_id: UUID
    action_type: str
    label: str
    detail: str
    old_value: str | None
    new_value: str | None
    trade_reference: str | None
    occurred_at: datetime


class ManualReconciliationResponse(BaseModel):
    stop_loss_changes: int
    take_profit_changes: int
    manual_closes: int
    actions: tuple[ManualActionResponse, ...]
    broker_trade_action_created: bool = False


def _service(request: Request) -> Day36ManualMt5ReconciliationService:
    existing = getattr(request.app.state, "day36_manual_reconciliation_service", None)
    if isinstance(existing, Day36ManualMt5ReconciliationService):
        return existing
    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "day36_mt5_runtime_unavailable",
                "message": "MT5 activity is temporarily unavailable.",
            },
        )
    service = Day36ManualMt5ReconciliationService(
        session_factory=base._session_factory,
        cipher=base._cipher,
        gateway=MetaApiReadGateway(),
    )
    request.app.state.day36_manual_reconciliation_service = service
    return service


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.get("/manual-actions", response_model=ManualReconciliationResponse)
async def manual_actions(
    request: Request,
    response: Response,
    identity: Identity,
) -> ManualReconciliationResponse:
    service = _service(request)
    try:
        result = await service.reconcile(identity["id"])
    except Day36ReconciliationError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE
                if exc.retryable
                else status.HTTP_502_BAD_GATEWAY
            ),
            detail={
                "code": exc.code,
                "message": "MT5 activity is temporarily unavailable.",
            },
        ) from exc
    _no_store(response)
    return ManualReconciliationResponse(
        stop_loss_changes=result.stop_loss_changes,
        take_profit_changes=result.take_profit_changes,
        manual_closes=result.manual_closes,
        actions=tuple(ManualActionResponse(**asdict(item)) for item in result.actions),
        broker_trade_action_created=False,
    )
