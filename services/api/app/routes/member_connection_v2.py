"""Production-stable asynchronous MT5 member connection flow.

Connection V2 deliberately separates the browser request from MetaAPI provisioning.
The owner submits the MT5 password once; it is held only in the in-process background
call and is never persisted. Progress and terminal results are persisted as sanitized
audit events so the UI can poll quickly without holding an HTTP request open.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.access_control import require_permission
from app.db import get_db_session, get_session_factory
from app.metaapi_gateway import MetaApiAccountState, MetaApiGatewayError, SUPER_SIGNALS_MAGIC
from app.models import AuditEvent, User
from app.mt5_account_profiles import Mt5AccountProfileService
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.routes.admin_accounts import router
from app.routes.member_onboarding_owner import _activate_verified_member, _normalize_email, _prepare_member

DbSession = Annotated[Session, Depends(get_db_session)]
OwnerUsers = Annotated[dict[str, Any], Depends(require_permission("users.manage"))]

_ATTEMPT_TTL_SECONDS = 300
_CREATE_DEADLINE_SECONDS = 120
_FIRST_CONNECT_WAIT_SECONDS = 45
_REDEPLOY_CONNECT_WAIT_SECONDS = 30


class ConnectionV2Request(BaseModel):
    mt5_password: SecretStr


class OnboardConnectionV2Request(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=120)
    mt5_login: str = Field(min_length=1, max_length=32)
    mt5_password: SecretStr
    mt5_server: str = Field(min_length=2, max_length=160)
    complimentary_access: Literal[True] = True


class ConnectionV2Accepted(BaseModel):
    user_id: UUID
    attempt_id: UUID
    status: Literal["connecting"] = "connecting"
    stage: str
    mt5_login_masked: str
    mt5_server: str


class ConnectionV2Status(BaseModel):
    user_id: UUID
    attempt_id: UUID | None
    status: str
    stage: str
    error_code: str | None
    mt5_status: str | None
    remote_state: str | None
    remote_connection_status: str | None
    trading_status: str | None
    risk_percent: float | None
    updated_at: datetime | None


def _owner(identity: dict[str, Any]) -> None:
    if identity.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Only the Owner can manage member MT5 connections.")


def _service(request: Request) -> Day30Mt5ConnectionService:
    service = require_mt5_service(request)
    if not isinstance(service, Day30Mt5ConnectionService):
        raise HTTPException(status_code=503, detail="Live MT5 connection service is unavailable.")
    return service


def _mask(login: str) -> str:
    return f"••••{login[-4:]}" if len(login) >= 4 else "••••"


def _audit(user_id: UUID, owner_id: UUID | None, event_type: str, **payload: object) -> None:
    with get_session_factory()() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type=event_type,
                entity_type="user",
                entity_id=user_id,
                payload={**payload, "trade_action_created": False},
            )
        )
        session.commit()


def _stop_trading(user_id: UUID) -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                """
                INSERT INTO user_trading_controls
                    (user_id, risk_percent, allow_double_lot, trading_status, activated_at, stopped_at, updated_at)
                VALUES (:user_id, 1.0, FALSE, 'stopped', NULL, now(), now())
                ON CONFLICT (user_id) DO UPDATE SET
                    risk_percent=1.0,
                    allow_double_lot=FALSE,
                    trading_status='stopped',
                    stopped_at=now(),
                    updated_at=now()
                """
            ),
            {"user_id": user_id},
        )
        session.commit()


def _mark_local_failure(user_id: UUID, error_code: str) -> None:
    with get_session_factory()() as session:
        session.execute(
            text(
                """
                UPDATE mt5_accounts
                SET status='error', last_error_code=:error_code,
                    last_checked_at=now(), updated_at=now()
                WHERE owner_user_id=:user_id
                """
            ),
            {"user_id": user_id, "error_code": error_code},
        )
        session.commit()


def _latest_attempt(session: Session, user_id: UUID) -> Any | None:
    return session.execute(
        text(
            """
            SELECT event_type, payload, created_at
            FROM audit_events
            WHERE entity_type='user'
              AND entity_id=:user_id
              AND event_type LIKE 'mt5.connection_v2.%'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()


def _ensure_no_live_attempt(session: Session, user_id: UUID) -> None:
    latest = _latest_attempt(session, user_id)
    if latest is None:
        return
    event_type = str(latest["event_type"])
    if event_type not in {"mt5.connection_v2.started", "mt5.connection_v2.stage"}:
        return
    created_at = latest["created_at"]
    if created_at is None:
        return
    age = (datetime.now(UTC) - created_at).total_seconds()
    if age < _ATTEMPT_TTL_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "mt5_connection_already_running",
                "message": "An MT5 connection attempt is already running for this member.",
            },
        )


async def _known_broker_name(service: Day30Mt5ConnectionService, token: str, server: str) -> str | None:
    response = await service._gateway._request(  # noqa: SLF001 - provisioning preflight
        "GET",
        "/known-mt-servers/5/search",
        token=token,
        params=[("query", server)],
    )
    payload = response.json() if response.content else {}
    if not isinstance(payload, dict):
        return None
    target = server.casefold()
    for broker, servers in payload.items():
        if not isinstance(servers, list):
            continue
        if any(str(item).casefold() == target for item in servers):
            return str(broker)
    return None


async def _matching_remote_accounts(
    service: Day30Mt5ConnectionService,
    token: str,
    *,
    login: str,
    server: str,
) -> list[MetaApiAccountState]:
    response = await service._gateway._request(  # noqa: SLF001 - provisioning inventory
        "GET",
        "/users/current/accounts",
        token=token,
        params=[("version", "5")],
    )
    payload = response.json() if response.content else []
    rows = payload.get("items", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise MetaApiGatewayError("metaapi_invalid_response")
    matches: list[MetaApiAccountState] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("login") or "") != login:
            continue
        if str(item.get("server") or "").casefold() != server.casefold():
            continue
        matches.append(service._gateway._account_state(item))  # noqa: SLF001
    return matches


async def _delete_remote(service: Day30Mt5ConnectionService, token: str, account_id: str) -> None:
    try:
        await service._gateway._request(  # noqa: SLF001 - stale terminal cleanup
            "DELETE",
            f"/users/current/accounts/{account_id}",
            token=token,
            params=[("executeForAllReplicas", "true")],
            accepted_statuses={204},
        )
    except MetaApiGatewayError as exc:
        if exc.code != "metaapi_account_not_found":
            raise


async def _create_remote(
    service: Day30Mt5ConnectionService,
    token: str,
    *,
    login: str,
    password: str,
    server: str,
    broker_name: str | None,
) -> MetaApiAccountState:
    transaction_id = service._gateway.new_transaction_id()  # noqa: SLF001
    deadline = time.monotonic() + _CREATE_DEADLINE_SECONDS
    body: dict[str, object] = {
        "login": login,
        "password": password,
        "name": "Smart Signals MT5",
        "server": server,
        "platform": "mt5",
        "magic": SUPER_SIGNALS_MAGIC,
        "type": "cloud-g2",
    }
    if broker_name:
        body["keywords"] = [broker_name]

    while True:
        response = await service._gateway._request(  # noqa: SLF001 - canonical create API
            "POST",
            "/users/current/accounts",
            token=token,
            transaction_id=transaction_id,
            json=body,
            accepted_statuses={201, 202},
        )
        if response.status_code == 201:
            payload = response.json() if response.content else {}
            if not isinstance(payload, dict) or not payload.get("id"):
                raise MetaApiGatewayError("metaapi_invalid_response")
            return await service._gateway.read_account(token=token, account_id=str(payload["id"]))

        retry_after = service._gateway._retry_after_seconds(response)  # noqa: SLF001
        if time.monotonic() + retry_after >= deadline:
            raise MetaApiGatewayError("metaapi_provisioning_timeout", retryable=True)
        await asyncio.sleep(retry_after)


async def _poll_connected(
    service: Day30Mt5ConnectionService,
    token: str,
    remote: MetaApiAccountState,
    *,
    seconds: int,
) -> MetaApiAccountState:
    if remote.state != "DEPLOYED":
        await service._gateway.deploy_account(token=token, account_id=remote.account_id)
    deadline = time.monotonic() + seconds
    latest = remote
    while time.monotonic() < deadline:
        latest = await service._gateway.read_account(token=token, account_id=remote.account_id)
        if latest.state in {"DEPLOY_FAILED", "REDEPLOY_FAILED"}:
            raise MetaApiGatewayError("metaapi_deploy_failed")
        if latest.state == "DEPLOYED" and latest.connection_status == "CONNECTED":
            return latest
        await asyncio.sleep(2)
    return latest


async def _run_connection_attempt(
    *,
    service: Day30Mt5ConnectionService,
    owner_id: UUID,
    user_id: UUID,
    attempt_id: UUID,
    login: str,
    password: str,
    server: str,
) -> None:
    token = ""
    new_remote: MetaApiAccountState | None = None
    try:
        _audit(user_id, owner_id, "mt5.connection_v2.stage", attempt_id=str(attempt_id), stage="server_preflight", status="connecting")
        token = service.resolve_platform_token()
        broker_name = await _known_broker_name(service, token, server)

        _audit(
            user_id,
            owner_id,
            "mt5.connection_v2.stage",
            attempt_id=str(attempt_id),
            stage="stale_terminal_cleanup",
            status="connecting",
            exact_broker_match=broker_name,
        )
        matches = await _matching_remote_accounts(service, token, login=login, server=server)
        for existing in matches:
            if existing.state == "DEPLOYED" and existing.connection_status == "CONNECTED":
                local_account_id = service._store_user_connection(  # noqa: SLF001
                    user_id=user_id,
                    token=token,
                    login=login,
                    server=server,
                    remote=existing,
                )
                del local_account_id
                with get_session_factory()() as session:
                    _activate_verified_member(session, owner_id=owner_id, user_id=user_id)
                Mt5AccountProfileService(
                    session_factory=get_session_factory(), connection_service=service
                ).sync_active_profile_from_canonical(user_id)
                _audit(
                    user_id,
                    owner_id,
                    "mt5.connection_v2.connected",
                    attempt_id=str(attempt_id),
                    stage="connected_existing",
                    status="connected",
                    remote_state=existing.state,
                    remote_connection_status=existing.connection_status,
                )
                return
            await _delete_remote(service, token, existing.account_id)

        _audit(user_id, owner_id, "mt5.connection_v2.stage", attempt_id=str(attempt_id), stage="provisioning", status="connecting")
        new_remote = await _create_remote(
            service,
            token,
            login=login,
            password=password,
            server=server,
            broker_name=broker_name,
        )

        _audit(
            user_id,
            owner_id,
            "mt5.connection_v2.stage",
            attempt_id=str(attempt_id),
            stage="broker_connect",
            status="connecting",
            remote_state=new_remote.state,
            remote_connection_status=new_remote.connection_status,
        )
        remote = await _poll_connected(
            service, token, new_remote, seconds=_FIRST_CONNECT_WAIT_SECONDS
        )
        if remote.connection_status != "CONNECTED":
            _audit(
                user_id,
                owner_id,
                "mt5.connection_v2.stage",
                attempt_id=str(attempt_id),
                stage="redeploy_verification",
                status="connecting",
                remote_state=remote.state,
                remote_connection_status=remote.connection_status,
            )
            await service._gateway._request(  # noqa: SLF001 - documented redeploy verification
                "POST",
                f"/users/current/accounts/{remote.account_id}/redeploy",
                token=token,
                accepted_statuses={200, 201, 202, 204},
            )
            remote = await _poll_connected(
                service, token, remote, seconds=_REDEPLOY_CONNECT_WAIT_SECONDS
            )

        if not (remote.state == "DEPLOYED" and remote.connection_status == "CONNECTED"):
            code = (
                "metaapi_disconnected_from_broker"
                if remote.connection_status == "DISCONNECTED_FROM_BROKER"
                else "metaapi_broker_session_not_established"
            )
            raise MetaApiGatewayError(code)

        service._store_user_connection(  # noqa: SLF001
            user_id=user_id,
            token=token,
            login=login,
            server=server,
            remote=remote,
        )
        with get_session_factory()() as session:
            _activate_verified_member(session, owner_id=owner_id, user_id=user_id)
        Mt5AccountProfileService(
            session_factory=get_session_factory(), connection_service=service
        ).sync_active_profile_from_canonical(user_id)
        _audit(
            user_id,
            owner_id,
            "mt5.connection_v2.connected",
            attempt_id=str(attempt_id),
            stage="connected",
            status="connected",
            remote_state=remote.state,
            remote_connection_status=remote.connection_status,
            broker_name=broker_name,
        )
    except (MetaApiGatewayError, Mt5ConnectionError) as exc:
        error_code = exc.code
        if token and new_remote is not None:
            try:
                await _delete_remote(service, token, new_remote.account_id)
            except Exception:
                pass
        _stop_trading(user_id)
        _mark_local_failure(user_id, error_code)
        _audit(
            user_id,
            owner_id,
            "mt5.connection_v2.failed",
            attempt_id=str(attempt_id),
            stage="failed",
            status="failed",
            error_code=error_code,
        )
    except Exception as exc:
        if token and new_remote is not None:
            try:
                await _delete_remote(service, token, new_remote.account_id)
            except Exception:
                pass
        _stop_trading(user_id)
        _mark_local_failure(user_id, "mt5_connection_internal_error")
        _audit(
            user_id,
            owner_id,
            "mt5.connection_v2.failed",
            attempt_id=str(attempt_id),
            stage="failed",
            status="failed",
            error_code="mt5_connection_internal_error",
            error_kind=type(exc).__name__,
        )


def _accepted(user_id: UUID, attempt_id: UUID, login: str, server: str) -> ConnectionV2Accepted:
    return ConnectionV2Accepted(
        user_id=user_id,
        attempt_id=attempt_id,
        stage="queued",
        mt5_login_masked=_mask(login),
        mt5_server=server,
    )


@router.post(
    "/members/{user_id}/connection-v2",
    response_model=ConnectionV2Accepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_member_connection_v2(
    user_id: UUID,
    payload: ConnectionV2Request,
    background_tasks: BackgroundTasks,
    request: Request,
    session: DbSession,
    identity: OwnerUsers,
) -> ConnectionV2Accepted:
    _owner(identity)
    service = _service(request)
    user = session.scalar(select(User).where(User.id == user_id, User.status == "active"))
    if user is None:
        raise HTTPException(status_code=404, detail="Active member not found.")
    row = session.execute(
        text(
            "SELECT login, server, account_environment FROM mt5_accounts "
            "WHERE owner_user_id=:user_id LIMIT 1"
        ),
        {"user_id": user_id},
    ).mappings().first()
    if row is None or str(row["account_environment"]).lower() != "live":
        raise HTTPException(status_code=409, detail="This member does not have a live MT5 account to reconnect.")
    _ensure_no_live_attempt(session, user_id)
    login = str(row["login"])
    server = str(row["server"])
    service.approve_user_account(
        approver_user_id=identity["id"], user_id=user_id, login=login, server=server
    )
    _stop_trading(user_id)
    attempt_id = uuid4()
    _audit(
        user_id,
        identity["id"],
        "mt5.connection_v2.started",
        attempt_id=str(attempt_id),
        stage="queued",
        status="connecting",
        login_last4=login[-4:],
        server=server,
    )
    background_tasks.add_task(
        _run_connection_attempt,
        service=service,
        owner_id=identity["id"],
        user_id=user_id,
        attempt_id=attempt_id,
        login=login,
        password=payload.mt5_password.get_secret_value(),
        server=server,
    )
    return _accepted(user_id, attempt_id, login, server)


@router.post(
    "/members/onboard-live-v2",
    response_model=ConnectionV2Accepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def onboard_live_member_v2(
    payload: OnboardConnectionV2Request,
    background_tasks: BackgroundTasks,
    request: Request,
    session: DbSession,
    identity: OwnerUsers,
) -> ConnectionV2Accepted:
    _owner(identity)
    service = _service(request)
    email = _normalize_email(payload.email)
    display_name = payload.display_name.strip()
    user_id, _created = _prepare_member(
        session,
        owner_id=identity["id"],
        email=email,
        display_name=display_name,
    )
    _ensure_no_live_attempt(session, user_id)
    login, server = service.validate_live_vantage_account(payload.mt5_login, payload.mt5_server)
    service.approve_user_account(
        approver_user_id=identity["id"], user_id=user_id, login=login, server=server
    )
    attempt_id = uuid4()
    _audit(
        user_id,
        identity["id"],
        "mt5.connection_v2.started",
        attempt_id=str(attempt_id),
        stage="queued",
        status="connecting",
        login_last4=login[-4:],
        server=server,
    )
    background_tasks.add_task(
        _run_connection_attempt,
        service=service,
        owner_id=identity["id"],
        user_id=user_id,
        attempt_id=attempt_id,
        login=login,
        password=payload.mt5_password.get_secret_value(),
        server=server,
    )
    return _accepted(user_id, attempt_id, login, server)


@router.get("/members/{user_id}/connection-v2", response_model=ConnectionV2Status)
def member_connection_v2_status(
    user_id: UUID,
    response: Response,
    session: DbSession,
    identity: OwnerUsers,
) -> ConnectionV2Status:
    _owner(identity)
    latest = _latest_attempt(session, user_id)
    row = session.execute(
        text(
            """
            SELECT a.status AS mt5_status, a.remote_state, a.remote_connection_status,
                   c.trading_status, c.risk_percent
            FROM mt5_accounts a
            LEFT JOIN user_trading_controls c ON c.user_id=a.owner_user_id
            WHERE a.owner_user_id=:user_id
            LIMIT 1
            """
        ),
        {"user_id": user_id},
    ).mappings().first()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if latest is None:
        return ConnectionV2Status(
            user_id=user_id,
            attempt_id=None,
            status="idle",
            stage="idle",
            error_code=None,
            mt5_status=str(row["mt5_status"]) if row else None,
            remote_state=str(row["remote_state"]) if row and row["remote_state"] else None,
            remote_connection_status=str(row["remote_connection_status"]) if row and row["remote_connection_status"] else None,
            trading_status=str(row["trading_status"]) if row and row["trading_status"] else None,
            risk_percent=float(row["risk_percent"]) if row and row["risk_percent"] is not None else None,
            updated_at=None,
        )

    payload = latest["payload"] if isinstance(latest["payload"], dict) else {}
    event_type = str(latest["event_type"])
    current_status = str(payload.get("status") or "connecting")
    stage = str(payload.get("stage") or "connecting")
    error_code = str(payload.get("error_code")) if payload.get("error_code") else None
    if event_type in {"mt5.connection_v2.started", "mt5.connection_v2.stage"}:
        age = (datetime.now(UTC) - latest["created_at"]).total_seconds()
        if age >= _ATTEMPT_TTL_SECONDS:
            current_status = "interrupted"
            stage = "interrupted"
            error_code = "mt5_connection_attempt_interrupted"
    elif event_type == "mt5.connection_v2.connected":
        current_status = "connected"
    elif event_type == "mt5.connection_v2.failed":
        current_status = "failed"

    attempt_raw = payload.get("attempt_id")
    try:
        attempt_id = UUID(str(attempt_raw)) if attempt_raw else None
    except ValueError:
        attempt_id = None
    return ConnectionV2Status(
        user_id=user_id,
        attempt_id=attempt_id,
        status=current_status,
        stage=stage,
        error_code=error_code,
        mt5_status=str(row["mt5_status"]) if row else None,
        remote_state=str(row["remote_state"]) if row and row["remote_state"] else None,
        remote_connection_status=str(row["remote_connection_status"]) if row and row["remote_connection_status"] else None,
        trading_status=str(row["trading_status"]) if row and row["trading_status"] else None,
        risk_percent=float(row["risk_percent"]) if row and row["risk_percent"] is not None else None,
        updated_at=latest["created_at"],
    )
