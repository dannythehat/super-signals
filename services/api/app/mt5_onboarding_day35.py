"""Member-to-Owner Vantage MT5 onboarding handshake for Day 35.

The member may submit only the MT5 login/account number and exact Vantage server.
The trading password is never stored or exposed to the Owner. After Owner approval,
the member enters only the trading password and the existing Day 30 connection service
uses the approved login/server to establish the MetaAPI connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text

from app.models import AuditEvent
from app.mt5_connection_service import Mt5ConnectionError, Mt5ConnectionView
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService, Mt5AccountApprovalView


@dataclass(frozen=True, slots=True)
class Mt5AccountRequestView:
    request_id: UUID | None
    user_id: UUID
    status: str
    login_masked: str | None
    server: str | None
    created_at: datetime | None
    updated_at: datetime | None
    reviewed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PendingMt5AccountRequestView:
    request_id: UUID
    user_id: UUID
    email: str
    display_name: str | None
    login_masked: str
    server: str
    requested_at: datetime
    updated_at: datetime


class Day35Mt5OnboardingService:
    def __init__(self, connection_service: Day30Mt5ConnectionService) -> None:
        self._connection_service = connection_service
        self._session_factory = connection_service._session_factory

    @staticmethod
    def _mask_login(login: str) -> str:
        value = login.strip()
        if len(value) <= 4:
            return f"••••{value}"
        return f"••••{value[-4:]}"

    def submit_request(self, *, user_id: UUID, login: str, server: str) -> Mt5AccountRequestView:
        normalized_login, normalized_server = self._connection_service.validate_live_vantage_account(
            login, server
        )
        now = datetime.now(UTC)
        with self._session_factory() as session:
            role = session.execute(
                text(
                    """
                    SELECT r.name
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id = u.id
                    JOIN roles AS r ON r.id = ur.role_id
                    WHERE u.id = :user_id
                      AND u.status = 'active'
                    ORDER BY CASE r.name WHEN 'user' THEN 0 ELSE 1 END
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).scalar_one_or_none()
            if role != "user":
                raise Mt5ConnectionError("mt5_user_not_eligible")

            active_approval = session.execute(
                text(
                    """
                    SELECT id
                    FROM mt5_account_approvals
                    WHERE user_id = :user_id AND status = 'active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).scalar_one_or_none()
            if active_approval is not None:
                raise Mt5ConnectionError("mt5_account_already_approved")

            request_id = session.execute(
                text(
                    """
                    INSERT INTO mt5_account_requests (
                        user_id, broker, platform, account_environment,
                        login, server, status, reviewed_by_user_id, reviewed_at,
                        created_at, updated_at
                    ) VALUES (
                        :user_id, 'vantage', 'mt5', 'live',
                        :login, :server, 'requested', NULL, NULL,
                        :now, :now
                    )
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        login = EXCLUDED.login,
                        server = EXCLUDED.server,
                        status = 'requested',
                        reviewed_by_user_id = NULL,
                        reviewed_at = NULL,
                        updated_at = EXCLUDED.updated_at
                    RETURNING id
                    """
                ),
                {
                    "user_id": user_id,
                    "login": normalized_login,
                    "server": normalized_server,
                    "now": now,
                },
            ).scalar_one()

            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.user_account_requested",
                    entity_type="mt5_account_request",
                    entity_id=request_id,
                    payload={
                        "broker": "vantage",
                        "platform": "mt5",
                        "account_environment": "live",
                        "login_last4": normalized_login[-4:],
                        "server": normalized_server,
                        "password_requested": False,
                        "metaapi_requested": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
        return self.get_request(user_id)

    def get_request(self, user_id: UUID) -> Mt5AccountRequestView:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, user_id, login, server, status,
                           created_at, updated_at, reviewed_at
                    FROM mt5_account_requests
                    WHERE user_id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return Mt5AccountRequestView(
                request_id=None,
                user_id=user_id,
                status="not_requested",
                login_masked=None,
                server=None,
                created_at=None,
                updated_at=None,
                reviewed_at=None,
            )
        return Mt5AccountRequestView(
            request_id=row["id"],
            user_id=row["user_id"],
            status=str(row["status"]),
            login_masked=self._mask_login(str(row["login"])),
            server=str(row["server"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            reviewed_at=row["reviewed_at"],
        )

    def list_pending_requests(self) -> list[PendingMt5AccountRequestView]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT req.id, req.user_id, u.email, u.display_name,
                           req.login, req.server, req.created_at, req.updated_at
                    FROM mt5_account_requests AS req
                    JOIN users AS u ON u.id = req.user_id
                    JOIN user_roles AS ur ON ur.user_id = u.id
                    JOIN roles AS r ON r.id = ur.role_id AND r.name = 'user'
                    WHERE req.status = 'requested'
                      AND u.status = 'active'
                    ORDER BY req.updated_at ASC, req.created_at ASC
                    """
                )
            ).mappings().all()
        return [
            PendingMt5AccountRequestView(
                request_id=row["id"],
                user_id=row["user_id"],
                email=str(row["email"]),
                display_name=row["display_name"],
                login_masked=self._mask_login(str(row["login"])),
                server=str(row["server"]),
                requested_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def approve_request(
        self, *, approver_user_id: UUID, user_id: UUID
    ) -> Mt5AccountApprovalView:
        with self._session_factory() as session:
            request_row = session.execute(
                text(
                    """
                    SELECT id, login, server, status
                    FROM mt5_account_requests
                    WHERE user_id = :user_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if request_row is None or str(request_row["status"]) != "requested":
            raise Mt5ConnectionError("mt5_request_not_pending")

        approval = self._connection_service.approve_user_account(
            approver_user_id=approver_user_id,
            user_id=user_id,
            login=str(request_row["login"]),
            server=str(request_row["server"]),
        )

        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE mt5_account_requests
                    SET status = 'approved',
                        reviewed_by_user_id = :approver_user_id,
                        reviewed_at = :now,
                        updated_at = :now
                    WHERE id = :request_id
                    """
                ),
                {
                    "approver_user_id": approver_user_id,
                    "now": now,
                    "request_id": request_row["id"],
                },
            )
            session.add(
                AuditEvent(
                    actor_user_id=approver_user_id,
                    event_type="mt5.user_account_request_approved",
                    entity_type="mt5_account_request",
                    entity_id=request_row["id"],
                    payload={
                        "user_id": str(user_id),
                        "login_last4": str(request_row["login"])[-4:],
                        "server": str(request_row["server"]),
                        "password_requested": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
        return approval

    async def connect_approved(self, *, user_id: UUID, password: str) -> Mt5ConnectionView:
        if not password or len(password) > 256:
            raise Mt5ConnectionError("mt5_password_invalid")
        with self._session_factory() as session:
            approval = session.execute(
                text(
                    """
                    SELECT login, server
                    FROM mt5_account_approvals
                    WHERE user_id = :user_id
                      AND status = 'active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if approval is None:
            raise Mt5ConnectionError("mt5_account_not_approved")
        return await self._connection_service.connect_user_live(
            user_id=user_id,
            login=str(approval["login"]),
            password=password,
            server=str(approval["server"]),
        )
