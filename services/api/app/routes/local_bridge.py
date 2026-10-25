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


__all__ = ["router"]
