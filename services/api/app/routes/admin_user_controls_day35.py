"""Day 35 Owner member management controls.

The historical global emergency-stop endpoints were removed on Day 40 after the
Owner locked the product to per-user Stop & Close plus Owner member revoke.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.access_control import require_permission
from app.admin_user_controls_day35 import (
    Day35AdminControlError,
    Day35AdminUserControlService,
)
from app.dashboard_day32 import QuietDay23Mt5ReadService
from app.dashboard_resilient_runtime import ResilientDashboardRuntimeService
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_runtime import require_mt5_service
from app.paper_resilient_read_gateway import ResilientMetaApiReadGateway
from app.trading_accounting import CanonicalTradingAccountingService
from app.trading_controls_day31 import Day31TradingControlService

router = APIRouter(prefix="/user-controls", tags=["day35-admin-controls"])
OwnerUsers = Annotated[dict[str, Any], Depends(require_permission("users.manage"))]


class ManagedUserResponse(BaseModel):
    user_id: UUID
    email: str
    display_name: str | None
    status: str
    trading_status: str | None
    risk_percent: Decimal | None
    allow_double_lot: bool | None
    mt5_status: str | None
    mt5_login_masked: str | None
    mt5_server: str | None
    mapped_open_positions: int
    mapped_pending_positions: int
    active_sessions: int
    push_devices_enabled: int


class ManagedAccountOverviewResponse(BaseModel):
    user_id: UUID
    display_name: str | None
    email: str
    role_name: str
    account_environment: str | None
    mt5_status: str | None
    remote_connection_status: str | None
    trading_status: str | None
    risk_percent: Decimal | None
    login_masked: str | None
    server: str | None
    currency: str | None
    balance: float | None
    equity: float | None
    free_margin: float | None
    open_profit: float | None
    realised_today: float | None
    realised_month: float | None
    realised_all_time: float | None
    mapped_open_positions: int
    mapped_pending_positions: int
    account_read_at: datetime | None


class RevokePreviewResponse(BaseModel):
    user: ManagedUserResponse
    confirmation_text: str
    confirmation_title: str
    confirmation_message: str
    mapped_positions_to_close: int
    manual_or_unmapped_positions_touched: bool
    broker_trade_action_created: bool


class ConfirmedActionRequest(BaseModel):
    confirmed: bool = False
    confirmation_text: str = Field(default="", max_length=400)


class RevokeResultResponse(BaseModel):
    user_id: UUID
    status: str
    automation_status: str
    broker_actions_sent: int
    mapped_positions_closed: int
    external_positions_reconciled: int
    sessions_revoked: int
    approvals_revoked: int
    push_devices_disabled: int
    manual_or_unmapped_positions_touched: bool


def _service(request: Request) -> Day35AdminUserControlService:
    existing = getattr(request.app.state, "day35_admin_user_control_service", None)
    if isinstance(existing, Day35AdminUserControlService):
        return existing

    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "day35_admin_controls_unavailable",
                "message": "Administrative trading controls are temporarily unavailable.",
            },
        )
    trading = Day31TradingControlService(
        session_factory=base._session_factory,
        cipher=base._cipher,
        read_gateway=MetaApiReadGateway(),
        trade_gateway=MetaApiTradeGateway(),
    )
    service = Day35AdminUserControlService(
        session_factory=base._session_factory,
        trading_service=trading,
    )
    request.app.state.day35_admin_user_control_service = service
    return service


def _portfolio_service(request: Request) -> ResilientDashboardRuntimeService:
    existing = getattr(request.app.state, "owner_account_portfolio_service", None)
    if isinstance(existing, ResilientDashboardRuntimeService):
        return existing

    base = require_mt5_service(request)
    if not isinstance(base, Day30Mt5ConnectionService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "owner_account_portfolio_unavailable",
                "message": "Account balances are temporarily unavailable.",
            },
        )
    read_service = QuietDay23Mt5ReadService(
        session_factory=base._session_factory,
        cipher=base._cipher,
        gateway=ResilientMetaApiReadGateway(timeout_seconds=2.5, attempts=1),
    )
    service = ResilientDashboardRuntimeService(
        session_factory=base._session_factory,
        read_service=read_service,
    )
    request.app.state.owner_account_portfolio_service = service
    return service


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _raise_admin_error(exc: Day35AdminControlError) -> None:
    messages = {
        "day35_managed_user_not_found": "That invited user was not found.",
        "day35_user_not_active": "Only an active invited user can be revoked from this control.",
        "day35_revoke_confirmation_required": "Type the exact revoke confirmation phrase before continuing.",
        "day35_owner_self_revoke_blocked": "The Owner account cannot revoke itself here.",
        "day35_user_state_changed": "The user's state changed while the revoke was running. Refresh before retrying.",
        "mt5_account_not_connected": "The user's approved Vantage MT5 account is not connected. Automation remains stopped; retry the account action after broker connectivity is restored.",
        "broker_credential_decryption_failed": "MT5 connectivity needs administrator recovery. Automation remains stopped.",
    }
    transient = {
        "mt5_account_not_connected",
        "metaapi_timeout",
        "metaapi_unreachable",
        "metaapi_temporarily_unavailable",
    }
    raise HTTPException(
        status_code=(
            status.HTTP_503_SERVICE_UNAVAILABLE
            if exc.retryable or exc.code in transient
            else status.HTTP_409_CONFLICT
        ),
        detail={
            "code": exc.code,
            "message": messages.get(
                exc.code,
                "The confirmed administrative action could not be completed safely.",
            ),
        },
    ) from exc


@router.get("/users", response_model=tuple[ManagedUserResponse, ...])
def managed_users(
    request: Request,
    response: Response,
    actor: OwnerUsers,
) -> tuple[ManagedUserResponse, ...]:
    del actor
    service = _service(request)
    rows = service.list_users()

    # A first-time Connection V2 attempt can fail before a remote MetaAPI terminal is
    # persisted. The Owner must still be able to retry from the existing member card,
    # so fall back to the approved live MT5 login/server instead of making the member
    # disappear from reconnect controls.
    with service._session_factory() as session:  # noqa: SLF001 - same route-owned service
        approval_rows = session.execute(
            text(
                """
                SELECT user_id, login, server
                FROM mt5_account_approvals
                WHERE status='active' AND account_environment='live'
                """
            )
        ).mappings().all()
    approvals = {item["user_id"]: item for item in approval_rows}

    payloads: list[ManagedUserResponse] = []
    for item in rows:
        data = asdict(item)
        approval = approvals.get(item.user_id)
        if data["mt5_login_masked"] is None and approval is not None:
            login = str(approval["login"])
            data["mt5_login_masked"] = f"••••{login[-4:]}" if len(login) >= 4 else "••••"
            data["mt5_server"] = str(approval["server"])
            data["mt5_status"] = "approved"
        payloads.append(ManagedUserResponse(**data))

    _no_store(response)
    return tuple(payloads)


@router.get("/accounts-overview", response_model=tuple[ManagedAccountOverviewResponse, ...])
async def managed_accounts_overview(
    request: Request,
    response: Response,
    actor: OwnerUsers,
    timezone_name: str = "UTC",
) -> tuple[ManagedAccountOverviewResponse, ...]:
    """Owner-only portfolio view across every active connected/configured MT5 account.

    Account reads reuse the same stale-while-revalidate service as the member dashboard:
    confirmed balances render immediately, while broker refreshes happen sparingly in the
    background. A newly connected account without a snapshot gets one bounded first read.
    """
    del actor
    dashboard = _portfolio_service(request)
    accounting = CanonicalTradingAccountingService(dashboard._session_factory)  # noqa: SLF001

    with dashboard._session_factory() as session:  # noqa: SLF001
        rows = session.execute(
            text(
                """
                WITH latest_accounts AS (
                    SELECT DISTINCT ON (owner_user_id)
                        owner_user_id,id,account_environment,status,remote_connection_status,
                        login,server,last_confirmed_currency,last_confirmed_balance,
                        last_confirmed_equity,last_confirmed_free_margin,last_confirmed_account_at
                    FROM mt5_accounts
                    WHERE status<>'revoked'
                    ORDER BY owner_user_id,created_at DESC
                )
                SELECT
                    u.id AS user_id,u.display_name,u.email,
                    COALESCE((
                        SELECT r.name
                        FROM user_roles ur
                        JOIN roles r ON r.id=ur.role_id
                        WHERE ur.user_id=u.id AND r.name IN ('owner','user')
                        ORDER BY CASE WHEN r.name='owner' THEN 0 ELSE 1 END
                        LIMIT 1
                    ),'user') AS role_name,
                    m.account_environment,m.status AS mt5_status,m.remote_connection_status,
                    m.login,m.server,m.last_confirmed_currency,m.last_confirmed_balance,
                    m.last_confirmed_equity,m.last_confirmed_free_margin,m.last_confirmed_account_at,
                    utc.trading_status,utc.risk_percent,
                    (SELECT COUNT(*)::int FROM positions p WHERE p.user_id=u.id AND p.status='open') AS mapped_open_positions,
                    (SELECT COUNT(*)::int FROM positions p WHERE p.user_id=u.id AND p.status='pending') AS mapped_pending_positions
                FROM users u
                JOIN latest_accounts m ON m.owner_user_id=u.id
                LEFT JOIN user_trading_controls utc ON utc.user_id=u.id
                WHERE u.status='active'
                ORDER BY CASE WHEN EXISTS (
                    SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
                    WHERE ur.user_id=u.id AND r.name='owner'
                ) THEN 0 ELSE 1 END,
                lower(COALESCE(u.display_name,u.email))
                """
            )
        ).mappings().all()

    semaphore = asyncio.Semaphore(4)

    async def read_one(row: Any) -> ManagedAccountOverviewResponse:
        user_id = UUID(str(row["user_id"]))
        view = None
        try:
            async with semaphore:
                view = await dashboard.read(user_id)
        except Exception:
            # Portfolio observability must never break the whole Owner page because one
            # broker account is temporarily unavailable. Durable confirmed values below
            # remain the fallback and no trading mutation is ever sent from this endpoint.
            view = None

        try:
            windows = accounting.windows(user_id, timezone_name=timezone_name)
            realised_today = float(windows.today)
            realised_month = float(windows.month)
            realised_all_time = float(windows.all_time)
        except Exception:
            realised_today = None
            realised_month = None
            realised_all_time = None

        live_account = view.account if view is not None else None
        balance = (
            float(live_account.balance)
            if live_account is not None
            else float(row["last_confirmed_balance"])
            if row["last_confirmed_balance"] is not None
            else None
        )
        equity = (
            float(live_account.equity)
            if live_account is not None
            else float(row["last_confirmed_equity"])
            if row["last_confirmed_equity"] is not None
            else None
        )
        free_margin = (
            float(live_account.free_margin)
            if live_account is not None
            else float(row["last_confirmed_free_margin"])
            if row["last_confirmed_free_margin"] is not None
            else None
        )
        currency = (
            str(live_account.currency)
            if live_account is not None
            else str(row["last_confirmed_currency"])
            if row["last_confirmed_currency"] is not None
            else None
        )
        login = str(row["login"] or "")
        connection_status = (
            view.connection.status if view is not None else str(row["mt5_status"] or "") or None
        )
        account_read_at = (
            view.connection.read_at
            if view is not None and view.connection.read_at is not None
            else row["last_confirmed_account_at"]
        )

        return ManagedAccountOverviewResponse(
            user_id=user_id,
            display_name=(str(row["display_name"]) if row["display_name"] else None),
            email=str(row["email"]),
            role_name=str(row["role_name"]),
            account_environment=str(row["account_environment"] or "") or None,
            mt5_status=connection_status,
            remote_connection_status=(
                str(row["remote_connection_status"])
                if row["remote_connection_status"] is not None
                else None
            ),
            trading_status=(str(row["trading_status"]) if row["trading_status"] else None),
            risk_percent=(Decimal(str(row["risk_percent"])) if row["risk_percent"] is not None else None),
            login_masked=(f"••••{login[-4:]}" if len(login) >= 4 else "••••" if login else None),
            server=(str(row["server"]) if row["server"] else None),
            currency=currency,
            balance=balance,
            equity=equity,
            free_margin=free_margin,
            open_profit=(float(view.open_profit) if view is not None and view.open_profit is not None else None),
            realised_today=realised_today,
            realised_month=realised_month,
            realised_all_time=realised_all_time,
            mapped_open_positions=int(row["mapped_open_positions"] or 0),
            mapped_pending_positions=int(row["mapped_pending_positions"] or 0),
            account_read_at=account_read_at,
        )

    payloads = await asyncio.gather(*(read_one(row) for row in rows))
    _no_store(response)
    return tuple(payloads)


@router.get("/users/{user_id}/revoke-preview", response_model=RevokePreviewResponse)
def revoke_preview(
    user_id: UUID,
    request: Request,
    response: Response,
    actor: OwnerUsers,
) -> RevokePreviewResponse:
    del actor
    try:
        view = _service(request).revoke_preview(user_id)
    except Day35AdminControlError as exc:
        _raise_admin_error(exc)
    _no_store(response)
    return RevokePreviewResponse(
        user=ManagedUserResponse(**asdict(view.user)),
        confirmation_text=view.confirmation_text,
        confirmation_title=view.confirmation_title,
        confirmation_message=view.confirmation_message,
        mapped_positions_to_close=view.mapped_positions_to_close,
        manual_or_unmapped_positions_touched=view.manual_or_unmapped_positions_touched,
        broker_trade_action_created=view.broker_trade_action_created,
    )


@router.post("/users/{user_id}/revoke", response_model=RevokeResultResponse)
async def revoke_user(
    user_id: UUID,
    payload: ConfirmedActionRequest,
    request: Request,
    response: Response,
    actor: OwnerUsers,
) -> RevokeResultResponse:
    try:
        result = await _service(request).revoke_user(
            user_id=user_id,
            actor_user_id=actor["id"],
            confirmed=payload.confirmed,
            confirmation_text=payload.confirmation_text,
        )
    except Day35AdminControlError as exc:
        _raise_admin_error(exc)
    _no_store(response)
    return RevokeResultResponse(**asdict(result))