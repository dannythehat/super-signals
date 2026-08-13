"""Day 35 Owner user controls and shared emergency-stop orchestration.

Dangerous operations reuse the accepted Day 31 mapped-position close path. They never
iterate arbitrary broker positions. Admin orchestration adds explicit confirmation,
actual-admin audit evidence and immediate access/session/push revocation where required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.trading_controls_day31 import (
    Day31StopResult,
    Day31TradingControlError,
    Day31TradingControlService,
)


class Day35AdminControlError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Day35ManagedUser:
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


@dataclass(frozen=True, slots=True)
class Day35RevokePreview:
    user: Day35ManagedUser
    confirmation_text: str
    confirmation_title: str
    confirmation_message: str
    mapped_positions_to_close: int
    manual_or_unmapped_positions_touched: bool = False
    broker_trade_action_created: bool = False


@dataclass(frozen=True, slots=True)
class Day35RevokeResult:
    user_id: UUID
    status: str
    automation_status: str
    broker_actions_sent: int
    mapped_positions_closed: int
    external_positions_reconciled: int
    sessions_revoked: int
    approvals_revoked: int
    push_devices_disabled: int
    manual_or_unmapped_positions_touched: bool = False


@dataclass(frozen=True, slots=True)
class Day35EmergencyPreview:
    active_users: int
    automation_active_users: int
    mapped_open_positions: int
    confirmation_text: str
    confirmation_title: str
    confirmation_message: str
    manual_or_unmapped_positions_touched: bool = False
    broker_trade_action_created: bool = False


@dataclass(frozen=True, slots=True)
class Day35EmergencyResult:
    users_targeted: int
    users_completed: int
    users_failed: int
    automation_users_stopped_first: int
    broker_actions_sent: int
    mapped_positions_closed: int
    external_positions_reconciled: int
    failures: tuple[dict[str, str], ...]
    manual_or_unmapped_positions_touched: bool = False


class Day35AdminUserControlService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        trading_service: Day31TradingControlService,
    ) -> None:
        self._session_factory = session_factory
        self._trading = trading_service

    def list_users(self) -> tuple[Day35ManagedUser, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        u.id AS user_id,
                        u.email::text AS email,
                        u.display_name,
                        u.status,
                        utc.trading_status,
                        utc.risk_percent,
                        utc.allow_double_lot,
                        ma.status AS mt5_status,
                        ma.login AS mt5_login,
                        ma.server AS mt5_server,
                        COUNT(DISTINCT p.id) FILTER (
                            WHERE p.status='open' AND p.broker_position_id IS NOT NULL
                        )::int AS mapped_open_positions,
                        COUNT(DISTINCT p.id) FILTER (
                            WHERE p.status='pending' AND p.broker_order_id IS NOT NULL
                        )::int AS mapped_pending_positions,
                        COUNT(DISTINCT s.id) FILTER (
                            WHERE s.revoked_at IS NULL AND s.expires_at>now()
                        )::int AS active_sessions,
                        COUNT(DISTINCT ps.id) FILTER (WHERE ps.enabled=true)::int AS push_devices_enabled
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN roles r ON r.id=ur.role_id AND r.name='user'
                    LEFT JOIN user_trading_controls utc ON utc.user_id=u.id
                    LEFT JOIN LATERAL (
                        SELECT status, login, server
                        FROM mt5_accounts
                        WHERE owner_user_id=u.id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) ma ON true
                    LEFT JOIN positions p ON p.user_id=u.id
                    LEFT JOIN auth_sessions s ON s.user_id=u.id
                    LEFT JOIN push_subscriptions ps ON ps.user_id=u.id
                    GROUP BY u.id,u.email,u.display_name,u.status,
                             utc.trading_status,utc.risk_percent,utc.allow_double_lot,
                             ma.status,ma.login,ma.server
                    ORDER BY
                        CASE u.status WHEN 'active' THEN 1 WHEN 'invited' THEN 2 WHEN 'suspended' THEN 3 ELSE 4 END,
                        lower(u.email::text)
                    """
                )
            ).mappings().all()
        return tuple(self._managed_user(row) for row in rows)

    def revoke_preview(self, user_id: UUID) -> Day35RevokePreview:
        user = self._user(user_id)
        if user.status != "active":
            raise Day35AdminControlError("day35_user_not_active")
        confirmation = f"REVOKE {user.email}"
        return Day35RevokePreview(
            user=user,
            confirmation_text=confirmation,
            confirmation_title=f"Revoke {user.email}?",
            confirmation_message=(
                "Super Signals will stop this user's automation before broker actions, close only "
                f"the {user.mapped_open_positions} currently mapped open Super Signals position(s), "
                "then revoke platform access, active sessions, MT5 approval and push delivery. "
                "Manual or otherwise unmapped MT5 positions are excluded."
            ),
            mapped_positions_to_close=user.mapped_open_positions,
        )

    async def revoke_user(
        self,
        *,
        user_id: UUID,
        actor_user_id: UUID,
        confirmed: bool,
        confirmation_text: str,
    ) -> Day35RevokeResult:
        preview = self.revoke_preview(user_id)
        if not confirmed or confirmation_text.strip() != preview.confirmation_text:
            raise Day35AdminControlError("day35_revoke_confirmation_required")
        if actor_user_id == user_id:
            raise Day35AdminControlError("day35_owner_self_revoke_blocked")

        try:
            stopped = await self._trading.stop_and_close(user_id)
        except Day31TradingControlError as exc:
            self._audit_admin_failure(
                actor_user_id=actor_user_id,
                user_id=user_id,
                event_type="admin.user_revoke_failed",
                error_code=exc.code,
            )
            raise Day35AdminControlError(exc.code, retryable=exc.retryable) from exc

        now = datetime.now(UTC)
        with self._session_factory() as session:
            sessions = session.execute(
                text(
                    """
                    UPDATE auth_sessions
                    SET revoked_at=COALESCE(revoked_at,:now)
                    WHERE user_id=:user_id AND revoked_at IS NULL
                    RETURNING id
                    """
                ),
                {"user_id": user_id, "now": now},
            ).all()
            approvals = session.execute(
                text(
                    """
                    UPDATE mt5_account_approvals
                    SET status='revoked', updated_at=:now
                    WHERE user_id=:user_id AND status='active'
                    RETURNING id
                    """
                ),
                {"user_id": user_id, "now": now},
            ).all()
            push = session.execute(
                text(
                    """
                    UPDATE push_subscriptions
                    SET enabled=false, updated_at=:now
                    WHERE user_id=:user_id AND enabled=true
                    RETURNING id
                    """
                ),
                {"user_id": user_id, "now": now},
            ).all()
            updated = session.execute(
                text(
                    """
                    UPDATE users
                    SET status='revoked', updated_at=:now
                    WHERE id=:user_id AND status='active'
                    RETURNING id
                    """
                ),
                {"user_id": user_id, "now": now},
            ).scalar_one_or_none()
            if updated is None:
                session.rollback()
                raise Day35AdminControlError("day35_user_state_changed")

            session.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    event_type="admin.user_revoked",
                    entity_type="user",
                    entity_id=user_id,
                    payload={
                        "affected_user_id": str(user_id),
                        "email": preview.user.email,
                        "automation_blocked_first": True,
                        "broker_actions_sent": stopped.broker_actions_sent,
                        "mapped_positions_closed": stopped.positions_closed,
                        "external_positions_reconciled": stopped.external_positions_reconciled,
                        "manual_or_unmapped_positions_touched": False,
                        "sessions_revoked": len(sessions),
                        "mt5_approvals_revoked": len(approvals),
                        "push_devices_disabled": len(push),
                        "confirmation_text_matched": True,
                    },
                )
            )
            session.commit()

        return Day35RevokeResult(
            user_id=user_id,
            status="revoked",
            automation_status="stopped",
            broker_actions_sent=stopped.broker_actions_sent,
            mapped_positions_closed=stopped.positions_closed,
            external_positions_reconciled=stopped.external_positions_reconciled,
            sessions_revoked=len(sessions),
            approvals_revoked=len(approvals),
            push_devices_disabled=len(push),
        )

    def emergency_preview(self) -> Day35EmergencyPreview:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        COUNT(DISTINCT u.id)::int AS active_users,
                        COUNT(DISTINCT u.id) FILTER (WHERE utc.trading_status='active')::int AS automation_active_users,
                        COUNT(DISTINCT p.id) FILTER (
                            WHERE p.status='open' AND p.broker_position_id IS NOT NULL
                        )::int AS mapped_open_positions
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN roles r ON r.id=ur.role_id AND r.name='user'
                    LEFT JOIN user_trading_controls utc ON utc.user_id=u.id
                    LEFT JOIN positions p ON p.user_id=u.id
                    WHERE u.status='active'
                    """
                )
            ).mappings().one()
        phrase = "STOP ALL SUPER SIGNALS"
        return Day35EmergencyPreview(
            active_users=int(row["active_users"] or 0),
            automation_active_users=int(row["automation_active_users"] or 0),
            mapped_open_positions=int(row["mapped_open_positions"] or 0),
            confirmation_text=phrase,
            confirmation_title="Emergency stop all Super Signals automation?",
            confirmation_message=(
                "All invited-user automation is blocked first. Super Signals then closes only mapped "
                f"bot positions ({int(row['mapped_open_positions'] or 0)} currently open). "
                "Manual or unmapped MT5 positions are never targeted. User accounts remain active."
            ),
        )

    async def emergency_stop(
        self,
        *,
        actor_user_id: UUID,
        confirmed: bool,
        confirmation_text: str,
    ) -> Day35EmergencyResult:
        preview = self.emergency_preview()
        if not confirmed or confirmation_text.strip() != preview.confirmation_text:
            raise Day35AdminControlError("day35_emergency_confirmation_required")

        user_ids = self._active_user_ids()
        now = datetime.now(UTC)
        # Emergency invariant: block every existing automation control before the
        # first broker close request. Users without a control row are already unable
        # to execute, and Day31 will materialise them as stopped if needed.
        with self._session_factory() as session:
            stopped_first = session.execute(
                text(
                    """
                    UPDATE user_trading_controls utc
                    SET trading_status='stopped', stopped_at=:now, updated_at=:now
                    WHERE utc.user_id=ANY(:user_ids)
                      AND utc.trading_status<>'stopped'
                    RETURNING user_id
                    """
                ),
                {"user_ids": list(user_ids), "now": now},
            ).all() if user_ids else []
            session.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    event_type="admin.emergency_stop_started",
                    entity_type="user_trading_controls",
                    payload={
                        "active_users_targeted": len(user_ids),
                        "automation_users_stopped_first": len(stopped_first),
                        "mapped_positions_expected": preview.mapped_open_positions,
                        "manual_or_unmapped_positions_touched": False,
                        "confirmation_text_matched": True,
                    },
                )
            )
            session.commit()

        broker_actions = 0
        positions_closed = 0
        reconciled = 0
        completed = 0
        failures: list[dict[str, str]] = []
        for user_id in user_ids:
            try:
                result = await self._trading.stop_and_close(user_id)
            except Day31TradingControlError as exc:
                failures.append({"user_id": str(user_id), "error_code": exc.code})
                continue
            broker_actions += result.broker_actions_sent
            positions_closed += result.positions_closed
            reconciled += result.external_positions_reconciled
            completed += 1

        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    event_type="admin.emergency_stop_completed",
                    entity_type="user_trading_controls",
                    payload={
                        "users_targeted": len(user_ids),
                        "users_completed": completed,
                        "users_failed": len(failures),
                        "automation_users_stopped_first": len(stopped_first),
                        "broker_actions_sent": broker_actions,
                        "mapped_positions_closed": positions_closed,
                        "external_positions_reconciled": reconciled,
                        "failures": failures,
                        "manual_or_unmapped_positions_touched": False,
                    },
                )
            )
            session.commit()

        return Day35EmergencyResult(
            users_targeted=len(user_ids),
            users_completed=completed,
            users_failed=len(failures),
            automation_users_stopped_first=len(stopped_first),
            broker_actions_sent=broker_actions,
            mapped_positions_closed=positions_closed,
            external_positions_reconciled=reconciled,
            failures=tuple(failures),
        )

    def _user(self, user_id: UUID) -> Day35ManagedUser:
        users = {item.user_id: item for item in self.list_users()}
        user = users.get(user_id)
        if user is None:
            raise Day35AdminControlError("day35_managed_user_not_found")
        return user

    def _active_user_ids(self) -> tuple[UUID, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                text(
                    """
                    SELECT u.id
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN roles r ON r.id=ur.role_id
                    WHERE r.name='user' AND u.status='active'
                    ORDER BY u.created_at,u.id
                    """
                )
            ).all()
        return tuple(value for value in rows if isinstance(value, UUID))

    def _audit_admin_failure(
        self,
        *,
        actor_user_id: UUID,
        user_id: UUID,
        event_type: str,
        error_code: str,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    event_type=event_type,
                    entity_type="user",
                    entity_id=user_id,
                    payload={
                        "affected_user_id": str(user_id),
                        "error_code": error_code,
                        "manual_or_unmapped_positions_touched": False,
                    },
                )
            )
            session.commit()

    @staticmethod
    def _managed_user(row: Any) -> Day35ManagedUser:
        login = str(row["mt5_login"] or "").strip()
        masked = None
        if login:
            masked = f"••••{login[-4:]}" if len(login) > 4 else "••••"
        return Day35ManagedUser(
            user_id=row["user_id"],
            email=str(row["email"]),
            display_name=(str(row["display_name"]) if row["display_name"] else None),
            status=str(row["status"]),
            trading_status=(str(row["trading_status"]) if row["trading_status"] else None),
            risk_percent=(Decimal(str(row["risk_percent"])) if row["risk_percent"] is not None else None),
            allow_double_lot=(bool(row["allow_double_lot"]) if row["allow_double_lot"] is not None else None),
            mt5_status=(str(row["mt5_status"]) if row["mt5_status"] else None),
            mt5_login_masked=masked,
            mt5_server=(str(row["mt5_server"]) if row["mt5_server"] else None),
            mapped_open_positions=int(row["mapped_open_positions"] or 0),
            mapped_pending_positions=int(row["mapped_pending_positions"] or 0),
            active_sessions=int(row["active_sessions"] or 0),
            push_devices_enabled=int(row["push_devices_enabled"] or 0),
        )


__all__ = [
    "Day35AdminControlError",
    "Day35AdminUserControlService",
    "Day35EmergencyPreview",
    "Day35EmergencyResult",
    "Day35ManagedUser",
    "Day35RevokePreview",
    "Day35RevokeResult",
]
