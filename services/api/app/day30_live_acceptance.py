"""Temporary dependency-free Day 30 acceptance on real Render Postgres.

Uses a deterministic fake MetaAPI provisioning gateway, so no real customer or
owner broker account is created/changed. The test MT5 password is ephemeral and
is never logged or persisted.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from uuid import uuid4

from sqlalchemy import text

from app.metaapi_gateway import (
    MetaApiAccountState,
    MetaApiCreateResult,
    MetaApiGatewayError,
)
from app.models import AuditEvent
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.routes.user_mt5_accounts import UserMt5ConnectRequest, UserMt5StatusResponse

logger = logging.getLogger(__name__)


class _FakeMetaApiGateway:
    def __init__(self, *, fail_first_created_read: bool = False) -> None:
        self.accounts: dict[tuple[str, str], MetaApiAccountState] = {}
        self.create_calls = 0
        self.find_calls = 0
        self.read_calls = 0
        self.deploy_calls = 0
        self.last_token: str | None = None
        self.last_password: str | None = None
        self._fail_first_created_read = fail_first_created_read
        self._failed_created_read = False

    @staticmethod
    def new_transaction_id() -> str:
        return uuid4().hex

    async def find_account(self, *, token: str, login: str, server: str):
        self.find_calls += 1
        self.last_token = token
        return self.accounts.get((login, server.casefold()))

    async def create_account(
        self,
        *,
        token: str,
        login: str,
        password: str,
        server: str,
        transaction_id: str,
    ) -> MetaApiCreateResult:
        del transaction_id
        self.create_calls += 1
        self.last_token = token
        self.last_password = password
        account = MetaApiAccountState(
            account_id=f"day30-{uuid4().hex}",
            login=login,
            server=server,
            state="CREATED",
            connection_status="DISCONNECTED",
        )
        self.accounts[(login, server.casefold())] = account
        return MetaApiCreateResult(pending=False, account_id=account.account_id, state="CREATED")

    async def read_account(self, *, token: str, account_id: str) -> MetaApiAccountState:
        self.read_calls += 1
        self.last_token = token
        for account in self.accounts.values():
            if account.account_id != account_id:
                continue
            if (
                self._fail_first_created_read
                and not self._failed_created_read
                and account.state == "CREATED"
            ):
                self._failed_created_read = True
                raise MetaApiGatewayError("metaapi_temporarily_unavailable", retryable=True)
            return account
        raise MetaApiGatewayError("metaapi_account_not_found")

    async def deploy_account(self, *, token: str, account_id: str) -> None:
        self.deploy_calls += 1
        self.last_token = token
        for key, account in tuple(self.accounts.items()):
            if account.account_id == account_id:
                self.accounts[key] = replace(
                    account,
                    state="DEPLOYED",
                    connection_status="CONNECTED",
                )
                return
        raise MetaApiGatewayError("metaapi_account_not_found")


def _expect_error(code: str, fn) -> None:
    try:
        fn()
    except Mt5ConnectionError as exc:
        if exc.code != code:
            raise AssertionError(f"expected {code}, got {exc.code}") from exc
        return
    raise AssertionError(f"expected {code}")


async def _expect_async_error(code: str, awaitable) -> None:
    try:
        await awaitable
    except Mt5ConnectionError as exc:
        if exc.code != code:
            raise AssertionError(f"expected {code}, got {exc.code}") from exc
        return
    raise AssertionError(f"expected {code}")


async def run_day30_live_acceptance(runtime_service: Day30Mt5ConnectionService) -> None:
    factory = runtime_service._session_factory
    with factory() as session:
        already = session.scalar(
            text(
                """
                SELECT count(*)
                FROM audit_events
                WHERE event_type = 'day30.live_acceptance_completed'
                  AND created_at > now() - interval '12 hours'
                """
            )
        )
        if already:
            logger.info("Day 30 live acceptance already completed recently; skipping")
            return

        owner_id = session.scalar(
            text(
                """
                SELECT u.id
                FROM users u
                JOIN user_roles ur ON ur.user_id = u.id
                JOIN roles r ON r.id = ur.role_id
                WHERE r.name = 'owner' AND u.status = 'active'
                ORDER BY u.created_at ASC
                LIMIT 1
                """
            )
        )
        user_role_id = session.scalar(text("SELECT id FROM roles WHERE name = 'user' LIMIT 1"))
        if owner_id is None or user_role_id is None:
            raise RuntimeError("Day 30 acceptance requires owner and user roles")

        marker = uuid4().hex[:10]
        user1_id = session.scalar(
            text(
                """
                INSERT INTO users (email, display_name, status)
                VALUES (:email, 'Day 30 Acceptance One', 'active')
                RETURNING id
                """
            ),
            {"email": f"day30-one-{marker}@example.invalid"},
        )
        user2_id = session.scalar(
            text(
                """
                INSERT INTO users (email, display_name, status)
                VALUES (:email, 'Day 30 Acceptance Two', 'active')
                RETURNING id
                """
            ),
            {"email": f"day30-two-{marker}@example.invalid"},
        )
        for user_id in (user1_id, user2_id):
            session.execute(
                text(
                    """
                    INSERT INTO user_roles (user_id, role_id, granted_by_user_id)
                    VALUES (:user_id, :role_id, :owner_id)
                    """
                ),
                {"user_id": user_id, "role_id": user_role_id, "owner_id": owner_id},
            )
        session.commit()

    platform_token = runtime_service.resolve_platform_token()
    password = f"day30-password-{marker}"
    server1 = "VantageInternational-Live"
    server2 = "VantageMarkets-Live"
    login1 = f"71{marker[:6].replace('a','1').replace('b','2').replace('c','3').replace('d','4').replace('e','5').replace('f','6')}"
    login2 = f"82{marker[4:].replace('a','1').replace('b','2').replace('c','3').replace('d','4').replace('e','5').replace('f','6')}"
    login1 = "".join(ch if ch.isdigit() else "7" for ch in login1)
    login2 = "".join(ch if ch.isdigit() else "8" for ch in login2)

    fake1 = _FakeMetaApiGateway()
    service1 = Day30Mt5ConnectionService(
        session_factory=factory,
        cipher=runtime_service._cipher,
        gateway=fake1,
    )

    # User request model must expose exactly the three broker fields and no MetaAPI/encryption field.
    if set(UserMt5ConnectRequest.model_fields) != {"login", "password", "server"}:
        raise AssertionError("normal-user MT5 connect request surface changed")
    forbidden_response_fields = {
        name
        for name in UserMt5StatusResponse.model_fields
        if any(term in name.casefold() for term in ("metaapi", "token", "cipher", "encrypt", "password"))
    }
    if forbidden_response_fields:
        raise AssertionError(f"secret platform fields exposed: {sorted(forbidden_response_fields)}")

    service1.approve_user_account(
        approver_user_id=owner_id,
        user_id=user1_id,
        login=login1,
        server=server1,
    )
    _expect_error(
        "mt5_demo_not_available",
        lambda: service1.validate_live_vantage_account(login1, "VantageMarkets-Demo"),
    )

    await _expect_async_error(
        "mt5_account_not_approved",
        service1.connect_user_live(
            user_id=user1_id,
            login=login1,
            password=password,
            server="VantageOther-Live",
        ),
    )

    connected1 = await service1.connect_user_live(
        user_id=user1_id,
        login=login1,
        password=password,
        server=server1,
    )
    if connected1.account_environment != "live" or connected1.status != "connected":
        raise AssertionError("approved live account did not connect")
    if fake1.create_calls != 1:
        raise AssertionError("approved account should create exactly one fake MetaAPI account")
    if fake1.last_token != platform_token:
        raise AssertionError("normal-user connection did not use central server-side token")
    if fake1.last_password != password:
        raise AssertionError("ephemeral MT5 password did not reach provisioning gateway")

    # Restart/recovery: new service instance, no password, same encrypted DB credential.
    restart_gateway = _FakeMetaApiGateway()
    restart_gateway.accounts = dict(fake1.accounts)
    restarted = Day30Mt5ConnectionService(
        session_factory=factory,
        cipher=runtime_service._cipher,
        gateway=restart_gateway,
    )
    refreshed = await restarted.refresh_user_live(user1_id)
    if refreshed.status != "connected" or restart_gateway.last_token != platform_token:
        raise AssertionError("restart refresh did not recover from encrypted server-side credential")
    if restart_gateway.create_calls != 0:
        raise AssertionError("refresh/reconciliation must never create a duplicate MetaAPI account")

    # Transient provisioning failure: remote creation happens once, retry finds and reuses it.
    fake2 = _FakeMetaApiGateway(fail_first_created_read=True)
    service2 = Day30Mt5ConnectionService(
        session_factory=factory,
        cipher=runtime_service._cipher,
        gateway=fake2,
    )
    service2.approve_user_account(
        approver_user_id=owner_id,
        user_id=user2_id,
        login=login2,
        server=server2,
    )
    await _expect_async_error(
        "metaapi_temporarily_unavailable",
        service2.connect_user_live(
            user_id=user2_id,
            login=login2,
            password=password,
            server=server2,
        ),
    )
    connected2 = await service2.connect_user_live(
        user_id=user2_id,
        login=login2,
        password=password,
        server=server2,
    )
    if connected2.status != "connected" or fake2.create_calls != 1:
        raise AssertionError("transient retry created a duplicate remote account")

    # The password must not appear in any persisted Day 30 account/approval/audit material.
    with factory() as session:
        leaked = session.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM mt5_accounts
                    WHERE owner_user_id IN (:user1, :user2)
                      AND to_jsonb(mt5_accounts)::text LIKE :needle
                ) OR EXISTS (
                    SELECT 1 FROM mt5_account_approvals
                    WHERE user_id IN (:user1, :user2)
                      AND to_jsonb(mt5_account_approvals)::text LIKE :needle
                ) OR EXISTS (
                    SELECT 1 FROM audit_events
                    WHERE actor_user_id IN (:user1, :user2, :owner_id)
                      AND created_at > now() - interval '10 minutes'
                      AND payload::text LIKE :needle
                )
                """
            ),
            {
                "user1": user1_id,
                "user2": user2_id,
                "owner_id": owner_id,
                "needle": f"%{password}%",
            },
        )
        if leaked:
            raise AssertionError("MT5 password leaked into PostgreSQL/audit payload")

        # Make acceptance artifacts inert before the real reconciliation manager starts.
        session.execute(
            text(
                """
                UPDATE mt5_accounts
                SET status = 'revoked', updated_at = now()
                WHERE owner_user_id IN (:user1, :user2)
                """
            ),
            {"user1": user1_id, "user2": user2_id},
        )
        session.execute(
            text(
                """
                UPDATE mt5_account_approvals
                SET status = 'revoked', updated_at = now()
                WHERE user_id IN (:user1, :user2)
                """
            ),
            {"user1": user1_id, "user2": user2_id},
        )
        session.execute(
            text(
                """
                UPDATE users SET status = 'revoked', updated_at = now()
                WHERE id IN (:user1, :user2)
                """
            ),
            {"user1": user1_id, "user2": user2_id},
        )
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="day30.live_acceptance_completed",
                entity_type="mt5_account_approval",
                payload={
                    "acceptance_version": "day30-live-2026-08-12",
                    "approved_account_connected": True,
                    "request_fields_login_password_server_only": True,
                    "metaapi_fields_exposed": False,
                    "password_persisted": False,
                    "restart_from_encrypted_credential": True,
                    "different_account_requires_approval": True,
                    "demo_rejected_for_normal_user": True,
                    "transient_retry_duplicate_metaapi_accounts": 0,
                    "refresh_create_calls": 0,
                    "temporary_users_revoked": True,
                },
            )
        )
        session.commit()

    logger.info(
        "Day 30 LIVE acceptance PASSED: approved_live=true password_persisted=false restart=true approval_gate=true demo_rejected=true duplicate_remote=0"
    )
