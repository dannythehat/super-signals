"""Dual Paper/Real MT5 onboarding and active-account selection.

A member may bind at most one Vantage demo account and one Vantage live account.
Connecting a second environment never silently changes the active trading target.
Switching the active environment is blocked while Smart Signals still has unfinished
positions for that user so provider management cannot be routed to the wrong broker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.models import AuditEvent
from app.mt5_connection_service import Mt5ConnectionError, Mt5ConnectionView
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService

_ENVIRONMENTS = {"demo", "live"}


@dataclass(frozen=True, slots=True)
class DualMt5AccountsView:
    active_account_environment: str | None
    paper: Mt5ConnectionView
    real: Mt5ConnectionView


def _empty_for(environment: str) -> Mt5ConnectionView:
    return Mt5ConnectionView(
        configured=False,
        account_id=None,
        broker="vantage",
        platform="mt5",
        account_environment=environment,
        login_masked=None,
        server=None,
        status="not_configured",
        remote_state=None,
        remote_connection_status=None,
        last_error_code=None,
        last_checked_at=None,
        last_connected_at=None,
    )


def _load_environment_row(
    service: Day30Mt5ConnectionService,
    user_id: UUID,
    environment: str,
) -> Any | None:
    with service._session_factory() as session:
        return session.execute(
            text(
                """
                SELECT *
                FROM mt5_accounts
                WHERE owner_user_id=:user_id
                  AND account_environment=:environment
                  AND status!='revoked'
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id, "environment": environment},
        ).mappings().first()


def _active_environment(service: Day30Mt5ConnectionService, user_id: UUID) -> str | None:
    with service._session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT active_account_environment
                FROM user_trading_controls
                WHERE user_id=:user_id
                """
            ),
            {"user_id": user_id},
        ).scalar_one_or_none()
    normalized = str(value or "").lower()
    return normalized if normalized in _ENVIRONMENTS else None


def get_dual_accounts(
    service: Day30Mt5ConnectionService,
    user_id: UUID,
) -> DualMt5AccountsView:
    demo_row = _load_environment_row(service, user_id, "demo")
    live_row = _load_environment_row(service, user_id, "live")
    return DualMt5AccountsView(
        active_account_environment=_active_environment(service, user_id),
        paper=service._view_from_row(demo_row) if demo_row is not None else _empty_for("demo"),
        real=service._view_from_row(live_row) if live_row is not None else _empty_for("live"),
    )


def _ensure_control(service: Day30Mt5ConnectionService, user_id: UUID) -> None:
    with service._session_factory() as session:
        session.execute(
            text(
                """
                INSERT INTO user_trading_controls (user_id)
                VALUES (:user_id)
                ON CONFLICT (user_id) DO NOTHING
                """
            ),
            {"user_id": user_id},
        )
        session.commit()


def _make_active_if_unset(
    service: Day30Mt5ConnectionService,
    user_id: UUID,
    environment: str,
) -> None:
    _ensure_control(service, user_id)
    now = datetime.now(UTC)
    with service._session_factory() as session:
        session.execute(
            text(
                """
                UPDATE user_trading_controls
                SET active_account_environment=COALESCE(active_account_environment, :environment),
                    updated_at=:now
                WHERE user_id=:user_id
                """
            ),
            {"user_id": user_id, "environment": environment, "now": now},
        )
        session.commit()


def set_active_environment(
    service: Day30Mt5ConnectionService,
    *,
    user_id: UUID,
    environment: str,
) -> DualMt5AccountsView:
    normalized = environment.strip().lower()
    if normalized not in _ENVIRONMENTS:
        raise Mt5ConnectionError("mt5_active_environment_invalid")

    target = _load_environment_row(service, user_id, normalized)
    if target is None or str(target["status"]) != "connected":
        raise Mt5ConnectionError("mt5_active_account_not_connected")

    current = _active_environment(service, user_id)
    if current == normalized:
        return get_dual_accounts(service, user_id)

    with service._session_factory() as session:
        unfinished = int(
            session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE user_id=:user_id
                      AND status NOT IN ('closed','skipped','error')
                    """
                ),
                {"user_id": user_id},
            ).scalar_one()
        )
        if unfinished:
            raise Mt5ConnectionError("mt5_active_switch_open_positions")

        _ensure_control(service, user_id)
        now = datetime.now(UTC)
        session.execute(
            text(
                """
                UPDATE user_trading_controls
                SET active_account_environment=:environment,
                    updated_at=:now
                WHERE user_id=:user_id
                """
            ),
            {"user_id": user_id, "environment": normalized, "now": now},
        )
        session.add(
            AuditEvent(
                actor_user_id=user_id,
                event_type="mt5.active_account_changed",
                entity_type="user_trading_controls",
                entity_id=user_id,
                payload={
                    "from": current,
                    "to": normalized,
                    "open_positions_blocked": True,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()
    return get_dual_accounts(service, user_id)


def _validate_demo(login: str, password: str, server: str) -> tuple[str, str]:
    normalized_login = login.strip()
    normalized_server = server.strip()
    if not normalized_login.isdigit() or len(normalized_login) > 32:
        raise Mt5ConnectionError("mt5_login_invalid")
    if not password or len(password) > 256:
        raise Mt5ConnectionError("mt5_password_invalid")
    folded = normalized_server.casefold()
    if len(normalized_server) < 2 or len(normalized_server) > 160:
        raise Mt5ConnectionError("mt5_server_invalid")
    if "vantage" not in folded:
        raise Mt5ConnectionError("mt5_vantage_server_required")
    if "demo" not in folded:
        raise Mt5ConnectionError("mt5_demo_server_required")
    return normalized_login, normalized_server


def _store_connection(
    service: Day30Mt5ConnectionService,
    *,
    user_id: UUID,
    token: str,
    login: str,
    server: str,
    environment: str,
    remote: Any,
) -> UUID:
    ciphertext = service._cipher.encrypt(token)
    fingerprint = service._cipher.fingerprint(token)
    local_status = service._local_status(remote)
    now = datetime.now(UTC)
    with service._session_factory() as session:
        row_id = session.execute(
            text(
                """
                INSERT INTO mt5_accounts (
                    owner_user_id, broker, platform, account_environment,
                    login, server, metaapi_account_id,
                    metaapi_token_ciphertext, metaapi_token_fingerprint,
                    status, remote_state, remote_connection_status,
                    last_error_code, last_checked_at, last_connected_at, updated_at
                ) VALUES (
                    :user_id, 'vantage', 'mt5', :environment,
                    :login, :server, :metaapi_account_id,
                    :ciphertext, :fingerprint,
                    :status, :remote_state, :remote_connection_status,
                    NULL, :now, :connected_at, :now
                )
                ON CONFLICT (owner_user_id, account_environment)
                DO UPDATE SET
                    broker='vantage', platform='mt5',
                    login=EXCLUDED.login,
                    server=EXCLUDED.server,
                    metaapi_account_id=EXCLUDED.metaapi_account_id,
                    metaapi_token_ciphertext=EXCLUDED.metaapi_token_ciphertext,
                    metaapi_token_fingerprint=EXCLUDED.metaapi_token_fingerprint,
                    status=EXCLUDED.status,
                    remote_state=EXCLUDED.remote_state,
                    remote_connection_status=EXCLUDED.remote_connection_status,
                    last_error_code=NULL,
                    last_checked_at=EXCLUDED.last_checked_at,
                    last_connected_at=COALESCE(EXCLUDED.last_connected_at, mt5_accounts.last_connected_at),
                    updated_at=EXCLUDED.updated_at
                RETURNING id
                """
            ),
            {
                "user_id": user_id,
                "environment": environment,
                "login": login,
                "server": server,
                "metaapi_account_id": remote.account_id,
                "ciphertext": ciphertext,
                "fingerprint": fingerprint,
                "status": local_status,
                "remote_state": remote.state,
                "remote_connection_status": remote.connection_status,
                "now": now,
                "connected_at": now if local_status == "connected" else None,
            },
        ).scalar_one()
        session.add(
            AuditEvent(
                actor_user_id=user_id,
                event_type=f"mt5.{environment}_connection_registered",
                entity_type="mt5_account",
                entity_id=row_id,
                payload={
                    "account_environment": environment,
                    "login_last4": login[-4:],
                    "server": server,
                    "password_stored": False,
                    "token_encrypted": True,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()
    return row_id


async def _provision_and_store(
    service: Day30Mt5ConnectionService,
    *,
    user_id: UUID,
    login: str,
    password: str,
    server: str,
    environment: str,
) -> Mt5ConnectionView:
    token = service.resolve_platform_token()
    try:
        remote = await service._gateway.find_account(token=token, login=login, server=server)
        if remote is None:
            remote = await service._provision_new_account(
                token=token,
                login=login,
                password=password,
                server=server,
            )
    except MetaApiGatewayError as exc:
        raise Mt5ConnectionError(exc.code) from exc

    local_id = _store_connection(
        service,
        user_id=user_id,
        token=token,
        login=login,
        server=server,
        environment=environment,
        remote=remote,
    )
    try:
        remote = await service._ensure_deployed_and_poll_connected(
            token=token,
            remote=remote,
            max_wait_seconds=75,
        )
        service._write_remote_state(local_id, remote)
    except MetaApiGatewayError as exc:
        if exc.code in service._TRANSIENT_CODES:
            service._mark_disconnected(local_id, exc.code)
        else:
            service._mark_error(local_id, exc.code)

    _make_active_if_unset(service, user_id, environment)
    row = _load_environment_row(service, user_id, environment)
    return service._view_from_row(row) if row is not None else _empty_for(environment)


async def connect_demo(
    service: Day30Mt5ConnectionService,
    *,
    user_id: UUID,
    login: str,
    password: str,
    server: str,
) -> Mt5ConnectionView:
    normalized_login, normalized_server = _validate_demo(login, password, server)
    return await _provision_and_store(
        service,
        user_id=user_id,
        login=normalized_login,
        password=password,
        server=normalized_server,
        environment="demo",
    )


def _approve_live(
    service: Day30Mt5ConnectionService,
    *,
    user_id: UUID,
    login: str,
    server: str,
) -> None:
    now = datetime.now(UTC)
    with service._session_factory() as session:
        previous = session.execute(
            text(
                """
                SELECT login, server
                FROM mt5_account_approvals
                WHERE user_id=:user_id
                FOR UPDATE
                """
            ),
            {"user_id": user_id},
        ).mappings().first()
        session.execute(
            text(
                """
                INSERT INTO mt5_account_approvals (
                    user_id, broker, platform, account_environment,
                    login, server, approved_by_user_id, status, approved_at, updated_at
                ) VALUES (
                    :user_id, 'vantage', 'mt5', 'live',
                    :login, :server, :user_id, 'active', :now, :now
                )
                ON CONFLICT (user_id)
                DO UPDATE SET login=EXCLUDED.login,
                              server=EXCLUDED.server,
                              approved_by_user_id=EXCLUDED.approved_by_user_id,
                              status='active',
                              approved_at=EXCLUDED.approved_at,
                              updated_at=EXCLUDED.updated_at
                """
            ),
            {"user_id": user_id, "login": login, "server": server, "now": now},
        )
        changed = previous is not None and (
            str(previous["login"]) != login
            or str(previous["server"]).casefold() != server.casefold()
        )
        if changed:
            # Replacing a Real account must never revoke the member's Paper account.
            session.execute(
                text(
                    """
                    UPDATE mt5_accounts
                    SET status='revoked',
                        last_error_code='mt5_owner_reapproval_changed',
                        last_checked_at=:now,
                        updated_at=:now
                    WHERE owner_user_id=:user_id
                      AND account_environment='live'
                    """
                ),
                {"user_id": user_id, "now": now},
            )
        session.commit()


async def connect_live(
    service: Day30Mt5ConnectionService,
    *,
    user_id: UUID,
    login: str,
    password: str,
    server: str,
) -> Mt5ConnectionView:
    normalized_login, normalized_server = service.validate_live_vantage_account(login, server)
    if not password or len(password) > 256:
        raise Mt5ConnectionError("mt5_password_invalid")
    _approve_live(
        service,
        user_id=user_id,
        login=normalized_login,
        server=normalized_server,
    )
    return await _provision_and_store(
        service,
        user_id=user_id,
        login=normalized_login,
        password=password,
        server=normalized_server,
        environment="live",
    )


async def refresh_environment(
    service: Day30Mt5ConnectionService,
    *,
    user_id: UUID,
    environment: str,
) -> Mt5ConnectionView:
    normalized = environment.strip().lower()
    if normalized not in _ENVIRONMENTS:
        raise Mt5ConnectionError("mt5_active_environment_invalid")
    row = _load_environment_row(service, user_id, normalized)
    if row is None:
        return _empty_for(normalized)
    await service._refresh_row_day22(row)
    refreshed = _load_environment_row(service, user_id, normalized)
    return service._view_from_row(refreshed) if refreshed is not None else _empty_for(normalized)
