"""Authenticated machine-to-machine API for the local Windows MT5 bridge."""

from __future__ import annotations

import hmac
import os
import re
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.db import get_session_factory
from app.local_bridge_queue import LocalBridgeQueue
from app.metaapi_gateway import MetaApiGatewayError
from app.pu_broker_readiness import assess_broker_preflight

router = APIRouter(prefix="/internal/local-bridge/v1", tags=["local-mt5-bridge"])
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$")


class WorkerIdentity(BaseModel):
    worker_id: str = Field(min_length=3, max_length=100)
    profile: str = Field(default="super-signals", min_length=3, max_length=80)
    account_id: str | None = Field(default=None, min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=40)
    capabilities: list[str] = Field(default_factory=list, max_length=40)


class ClaimRequest(WorkerIdentity):
    lease_seconds: int = Field(default=45, ge=15, le=120)


class CommandResponse(BaseModel):
    id: UUID
    operation: str
    account_id: str
    payload: dict[str, Any]
    lease_token: UUID
    attempt_count: int


class ClaimResponse(BaseModel):
    command: CommandResponse | None


class CompletionRequest(BaseModel):
    worker_id: str = Field(min_length=3, max_length=100)
    lease_token: UUID
    status: Literal["succeeded", "failed", "ambiguous"]
    result: Any = None
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str | None = Field(default=None, max_length=1000)


def _authorize(authorization: Annotated[str | None, Header()] = None) -> str:
    """Choose exactly one profile scope from an independently configured bearer token."""
    supplied = (
        authorization[7:]
        if authorization and authorization.startswith("Bearer ")
        else ""
    )
    legacy = os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN", "").strip()
    pu = os.getenv("SUPER_SIGNALS_PU_BRIDGE_TOKEN", "").strip()
    # A shared token would erase the identity boundary; fail closed on bad setup.
    if len(pu) >= 32 and pu != legacy and hmac.compare_digest(supplied, pu):
        return "pu"
    if len(legacy) >= 32 and hmac.compare_digest(supplied, legacy):
        return "legacy"
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _scope(identity: WorkerIdentity | None, authorization_scope: str) -> tuple[str, str | None]:
    if identity is not None:
        if not _IDENTIFIER.fullmatch(identity.worker_id):
            raise HTTPException(status_code=422, detail="worker_id_invalid")
        if not _IDENTIFIER.fullmatch(identity.profile):
            raise HTTPException(status_code=422, detail="profile_invalid")
    if authorization_scope == "pu":
        profile = os.getenv("SUPER_SIGNALS_PU_BRIDGE_PROFILE", "pu-prime").strip()
        account_id = os.getenv("SUPER_SIGNALS_PU_BRIDGE_ACCOUNT_ID", "").strip()
        if not profile or not account_id:
            raise HTTPException(status_code=503, detail="pu_bridge_not_configured")
        if identity is not None and (
            identity.profile != profile or identity.account_id != account_id
        ):
            raise HTTPException(status_code=403, detail="pu_bridge_scope_denied")
        return profile, account_id
    profile = os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals").strip()
    if identity is not None and identity.profile != profile:
        raise HTTPException(status_code=403, detail="profile_not_allowed")
    return profile, None


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def heartbeat(
    payload: WorkerIdentity,
    authorization_scope: Annotated[str, Depends(_authorize)],
) -> Response:
    profile, account_id = _scope(payload, authorization_scope)
    queue = LocalBridgeQueue(get_session_factory(), profile=profile, account_id=account_id)
    # This endpoint never receives or persists an MT5 password or broker secret.
    queue.heartbeat(**payload.model_dump(exclude={"account_id"}))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/claim", response_model=ClaimResponse)
def claim(
    payload: ClaimRequest,
    authorization_scope: Annotated[str, Depends(_authorize)],
) -> ClaimResponse:
    profile, account_id = _scope(payload, authorization_scope)
    queue = LocalBridgeQueue(get_session_factory(), profile=profile, account_id=account_id)
    queue.heartbeat(
        worker_id=payload.worker_id,
        profile=payload.profile,
        version=payload.version,
        capabilities=payload.capabilities,
    )
    command = queue.claim(
        worker_id=payload.worker_id,
        profile=payload.profile,
        lease_seconds=payload.lease_seconds,
    )
    if command is None:
        return ClaimResponse(command=None)
    assert command.lease_token is not None
    return ClaimResponse(
        command=CommandResponse(
            id=command.id,
            operation=command.operation,
            account_id=command.account_id,
            payload=command.payload,
            lease_token=command.lease_token,
            attempt_count=command.attempt_count,
        )
    )


@router.post("/commands/{command_id}/complete")
def complete(
    command_id: UUID,
    payload: CompletionRequest,
    authorization_scope: Annotated[str, Depends(_authorize)],
) -> dict[str, str]:
    if not _IDENTIFIER.fullmatch(payload.worker_id):
        raise HTTPException(status_code=422, detail="worker_id_invalid")
    profile, account_id = _scope(None, authorization_scope)
    queue = LocalBridgeQueue(
        get_session_factory(),
        profile=profile,
        account_id=account_id,
    )
    try:
        final_status = queue.complete(
            command_id=command_id,
            worker_id=payload.worker_id,
            lease_token=payload.lease_token,
            status=payload.status,
            result=payload.result,
            error_code=payload.error_code,
            error_message=payload.error_message,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": final_status}

@router.post("/pu-smoke")
async def pu_connection_smoke(
    authorization_scope: Annotated[str, Depends(_authorize)],
) -> dict[str, Any]:
    """Operator-triggered read-only cloud -> queue -> Contabo -> MT5 probe."""
    if authorization_scope != "pu":
        raise HTTPException(status_code=403, detail="pu_token_required")
    profile, account_id = _scope(None, authorization_scope)
    assert account_id is not None
    queue = LocalBridgeQueue(
        get_session_factory(), profile=profile, account_id=account_id,
        timeout_seconds=20.0,
    )
    try:
        result = await queue.execute(
            account_id=account_id,
            operation="read_terminal_identity",
            payload={},
        )
    except MetaApiGatewayError as exc:
        raise HTTPException(
            status_code=503, detail="pu_bridge_worker_unavailable_or_failed"
        ) from exc
    if not isinstance(result, dict):
        raise HTTPException(status_code=503, detail="pu_bridge_probe_invalid_result")
    return {"status": "connected", "terminal": result}


@router.post("/pu-preflight")
async def pu_broker_preflight(
    authorization_scope: Annotated[str, Depends(_authorize)],
) -> dict[str, Any]:
    """Read only: inspect live PU identity, balance, trade permissions and exposure.

    This endpoint never enqueues a trade. Even 'passed' here does NOT mean
    provider->PU dispatch or copier mirroring is operational.
    """
    if authorization_scope != "pu":
        raise HTTPException(status_code=403, detail="pu_token_required")
    profile, account_id = _scope(None, authorization_scope)
    assert account_id is not None
    queue = LocalBridgeQueue(
        get_session_factory(), profile=profile, account_id=account_id,
        timeout_seconds=20.0,
    )
    try:
        results = {}
        for operation, payload in (
            ("read_terminal_identity", {}),
            ("read_account_information", {}),
            ("read_symbol_specification", {"symbol": "XAUUSD"}),
            ("read_positions", {}),
            ("read_orders", {}),
        ):
            results[operation] = await queue.execute(
                account_id=account_id,
                operation=operation,
                payload=payload,
            )
    except MetaApiGatewayError as exc:
        raise HTTPException(
            status_code=503, detail="pu_broker_preflight_unavailable"
        ) from exc
    identity = results["read_terminal_identity"]
    account = results["read_account_information"]
    symbol = results["read_symbol_specification"]
    positions = results["read_positions"]
    orders = results["read_orders"]
    if not all(isinstance(item, dict) for item in (identity, account, symbol)):
        raise HTTPException(status_code=503, detail="pu_broker_preflight_invalid_payload")
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise HTTPException(status_code=503, detail="pu_broker_preflight_invalid_exposure")
    return assess_broker_preflight(
        identity=identity,
        account=account,
        symbol=symbol,
        positions=positions,
        orders=orders,
    )



# Owner-selected PU Prime DEMO source feed. This is read-only and never
# creates broker trades or alters global source status.
_OWNER_DEMO_CHAT_IDS = (
    -1001313672961, -1002176701424, -1001651583302,
    -1001588519179, -1002625307834, -1001649736978,
    -1001391285381, -1003368777331, -1002148698027,
    -1001957768371,
)

@router.get("/owner-demo-700207861-feed")
def owner_demo_700207861_feed(
    authorization_scope: Annotated[str, Depends(_authorize)],
) -> dict[str, Any]:
    """Fresh accepted *entry* signals for the isolated owner DEMO executor.

    It deliberately includes approved testing/shadow sources without promoting
    their general site-wide trading status. No historical playback and no
    execution permissions are conferred by this endpoint.
    """
    if authorization_scope != "legacy":
        raise HTTPException(status_code=403, detail="legacy_bridge_token_required")
    from sqlalchemy import text as sql_text

    with get_session_factory()() as session:
        rows = session.execute(
            sql_text(
                """
                SELECT sig.id::text AS signal_id, src.chat_id,
                       sig.side, sig.symbol, sig.order_type,
                       sig.entry_low, sig.entry_high, sig.stop_loss,
                       sig.take_profits, sig.has_open_runner,
                       sig.created_at, sig.source_posted_at
                FROM signals sig JOIN sources src ON sig.source_id=src.id
                WHERE src.chat_id IN (
                    -1001313672961, -1002176701424, -1001651583302,
                    -1001588519179, -1002625307834, -1001649736978,
                    -1001391285381, -1003368777331, -1002148698027,
                    -1001957768371
                )
                  AND src.status IN ('testing', 'live', 'shadow')
                  AND sig.parser_status='accepted'
                  AND sig.order_type='market'
                  AND sig.symbol='XAUUSD'
                  AND sig.side IN ('BUY', 'SELL')
                  AND sig.created_at >= now() - INTERVAL '100 seconds'
                  AND sig.source_posted_at >= now() - INTERVAL '150 seconds'
                  AND sig.created_at <= now() + INTERVAL '5 seconds'
                  AND sig.source_posted_at <= now() + INTERVAL '5 seconds'
                ORDER BY sig.created_at DESC, sig.id DESC
                LIMIT 30
                """
            )
        ).mappings().all()
    def number(value: object) -> float | None:
        return None if value is None else float(value)

    return {
        "feed": "owner-demo-700207861",
        "trading_permission": False,
        "signals": [
            {
                "id": str(r["signal_id"]),
                "chat_id": int(r["chat_id"]),
                "side": str(r["side"]),
                "symbol": str(r["symbol"]),
                "order_type": str(r["order_type"]),
                "entry_low": number(r["entry_low"]),
                "entry_high": number(r["entry_high"]),
                "stop_loss": number(r["stop_loss"]),
                "take_profits": list(r["take_profits"] or []),
                "has_open_runner": bool(r["has_open_runner"]),
                "created_at": r["created_at"].isoformat(),
                "source_posted_at": r["source_posted_at"].isoformat() if r["source_posted_at"] else None,
            }
            for r in rows
        ],
    }

__all__ = ["router"]
