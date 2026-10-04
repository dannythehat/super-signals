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

router = APIRouter(prefix="/internal/local-bridge/v1", tags=["local-mt5-bridge"])
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$")


class WorkerIdentity(BaseModel):
    worker_id: str = Field(min_length=3, max_length=100)
    profile: str = Field(default="super-signals", min_length=3, max_length=80)
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


def _authorize(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN", "")
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[7:]
    if len(expected) < 32 or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def _validate_identity(identity: WorkerIdentity) -> None:
    if not _IDENTIFIER.fullmatch(identity.worker_id):
        raise HTTPException(status_code=422, detail="worker_id_invalid")
    if not _IDENTIFIER.fullmatch(identity.profile):
        raise HTTPException(status_code=422, detail="profile_invalid")
    expected_profile = os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals").strip()
    if identity.profile != expected_profile:
        raise HTTPException(status_code=403, detail="profile_not_allowed")


@router.post("/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
def heartbeat(
    payload: WorkerIdentity,
    _: Annotated[None, Depends(_authorize)],
) -> Response:
    _validate_identity(payload)
    queue = LocalBridgeQueue(get_session_factory(), profile=payload.profile)
    # This endpoint never receives or persists an MT5 login, password or server secret.
    queue.heartbeat(**payload.model_dump())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/claim", response_model=ClaimResponse)
def claim(
    payload: ClaimRequest,
    _: Annotated[None, Depends(_authorize)],
) -> ClaimResponse:
    _validate_identity(payload)
    queue = LocalBridgeQueue(get_session_factory(), profile=payload.profile)
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
    _: Annotated[None, Depends(_authorize)],
) -> dict[str, str]:
    if not _IDENTIFIER.fullmatch(payload.worker_id):
        raise HTTPException(status_code=422, detail="worker_id_invalid")
    queue = LocalBridgeQueue(
        get_session_factory(),
        profile=os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals"),
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


__all__ = ["router"]
