"""Saved Demo/Real Vantage MT5 profiles with one canonical active execution slot."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.models import AuditEvent
from app.mt5_connection_service import Mt5ConnectionError
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService


class Mt5AccountProfileService:
    """Keep Demo and Real connections saved while mt5_accounts remains the active slot.

    The execution engine intentionally continues reading exactly one row from mt5_accounts.
    Switching accounts copies a previously connected profile into that canonical row only
    after Smart Signals has confirmed there is no locally mapped open/pending exposure.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        connection_service: Day30Mt5ConnectionService,
    ) -> None:
        self._session_factory = session_factory
        self._connection = connection_service

    @staticmethod
    def environment_for_server(server: str) -> str:
        normalized = server.strip()
        folded = normalized.casefold()
        if "vantage" not in folded:
            raise Mt5ConnectionError("mt5_vantage_server_required")
        return "demo" if "demo" in folded else "live"

    @staticmethod
    def _mask_login(login: str) -> str:
        if len(login) <= 4:
            return "*" * len(login)
        return f"{'*' * max(4, len(login) - 4)}{login[-4:]}"

    def list_profiles(self, user_id: UUID) -> dict[str, Any]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, account_environment, login, server, status,
                           remote_state, remote_connection_status, last_error_code,
                           last_checked_at, last_connected_at, is_active
                    FROM mt5_account_profiles
                    WHERE user_id=:user_id AND status<>'revoked'
                    ORDER BY CASE account_environment WHEN 'demo' THEN 0 ELSE 1 END
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
            exposure = int(
                session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM positions
                        WHERE user_id=:user_id
                          AND status IN ('open','pending')
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one()
            )

        profiles = [
            {
                "id": str(row["id"]),
                "account_environment": str(row["account_environment"]),
                "label": "Paper / Demo" if str(row["account_environment"]) == "demo" else "Real",
                "login_masked": self._mask_login(str(row["login"])),
                "server": str(row["server"]),
                "status": str(row["status"]),
                "remote_state": row["remote_state"],
                "remote_connection_status": row["remote_connection_status"],
                "last_error_code": row["last_error_code"],
                "last_checked_at": row["last_checked_at"],
                "last_connected_at": row["last_connected_at"],
                "active": bool(row["is_active"]),
            }
            for row in rows
        ]
        active = next((item["account_environment"] for item in profiles if item["active"]), None)
        return {
            "active_environment": active,
            "switch_blocked": exposure > 0,
            "switch_block_reason": "open_or_pending_trades" if exposure > 0 else None,
            "open_or_pending_position_count": exposure,
            "profiles": profiles,
        }

    def sync_active_profile_from_canonical(self, user_id: UUID) -> None:
        """Mirror a legacy/current canonical connection into saved profiles."""
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT * FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status<>'revoked'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
            if row is None:
                return
            session.execute(
                text("UPDATE mt5_account_profiles SET is_active=FALSE, updated_at=now() WHERE user_id=:user_id"),
                {"user_id": user_id},
            )
            session.execute(
                text(
                    """
                    INSERT INTO mt5_account_profiles (
                        user_id, broker, platform, account_environment, login, server,
                        metaapi_account_id, metaapi_token_ciphertext, metaapi_token_fingerprint,
                        status, remote_state, remote_connection_status, last_error_code,
                        last_checked_at, last_connected_at, is_active, updated_at
                    ) VALUES (
                        :user_id, :broker, :platform, :environment, :login, :server,
                        :metaapi_account_id, :ciphertext, :fingerprint,
                        :status, :remote_state, :remote_connection_status, :last_error_code,
                        :last_checked_at, :last_connected_at, TRUE, now()
                    )
                    ON CONFLICT (user_id, account_environment)
                    DO UPDATE SET
                        broker=EXCLUDED.broker,
                        platform=EXCLUDED.platform,
                        login=EXCLUDED.login,
                        server=EXCLUDED.server,
                        metaapi_account_id=EXCLUDED.metaapi_account_id,
                        metaapi_token_ciphertext=EXCLUDED.metaapi_token_ciphertext,
                        metaapi_token_fingerprint=EXCLUDED.metaapi_token_fingerprint,
                        status=EXCLUDED.status,
                        remote_state=EXCLUDED.remote_state,
                        remote_connection_status=EXCLUDED.remote_connection_status,
                        last_error_code=EXCLUDED.last_error_code,
                        last_checked_at=EXCLUDED.last_checked_at,
                        last_connected_at=EXCLUDED.last_connected_at,
                        is_active=TRUE,
                        updated_at=now()
                    """
                ),
                {
                    "user_id": user_id,
                    "broker": row["broker"],
                    "platform": row["platform"],
                    "environment": row["account_environment"],
                    "login": row["login"],
                    "server": row["server"],
                    "metaapi_account_id": row["metaapi_account_id"],
                    "ciphertext": row["metaapi_token_ciphertext"],
                    "fingerprint": row["metaapi_token_fingerprint"],
                    "status": row["status"],
                    "remote_state": row["remote_state"],
                    "remote_connection_status": row["remote_connection_status"],
                    "last_error_code": row["last_error_code"],
                    "last_checked_at": row["last_checked_at"],
                    "last_connected_at": row["last_connected_at"],
                },
            )
            session.commit()

    async def connect_profile(
        self,
        *,
        user_id: UUID,
        role: str,
        login: str,
        password: str,
        server: str,
        make_active: bool = False,
    ) -> dict[str, Any]:
        normalized_login = login.strip()
        normalized_server = server.strip()
        environment = self.environment_for_server(normalized_server)
        token = self._connection.resolve_platform_token()

        if environment == "demo":
            self._connection._validate_input(  # noqa: SLF001 - shared validated MT5 primitive
                token, normalized_login, password, normalized_server
            )
        else:
            normalized_login, normalized_server = self._connection.validate_live_vantage_account(
                normalized_login, normalized_server
            )
            if not password or len(password) > 256:
                raise Mt5ConnectionError("mt5_password_invalid")
            if role == "user":
                # Keep the existing approval/audit invariant for ordinary member LIVE accounts.
                self._connection.approve_user_account(
                    approver_user_id=user_id,
                    user_id=user_id,
                    login=normalized_login,
                    server=normalized_server,
                )

        try:
            remote = await self._connection._gateway.find_account(  # noqa: SLF001
                token=token,
                login=normalized_login,
                server=normalized_server,
            )
            if remote is None:
                remote = await self._connection._provision_new_account(  # noqa: SLF001
                    token=token,
                    login=normalized_login,
                    password=password,
                    server=normalized_server,
                )
            remote = await self._connection._ensure_deployed_and_poll_connected(  # noqa: SLF001
                token=token,
                remote=remote,
                max_wait_seconds=75,
            )
        except MetaApiGatewayError as exc:
            raise Mt5ConnectionError(exc.code) from exc

        local_status = self._connection._local_status(remote)  # noqa: SLF001
        ciphertext = self._connection._cipher.encrypt(token)  # noqa: SLF001
        fingerprint = self._connection._cipher.fingerprint(token)  # noqa: SLF001
        now = datetime.now(UTC)
        with self._session_factory() as session:
            existing_active = bool(
                session.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM mt5_account_profiles WHERE user_id=:user_id AND is_active)"
                    ),
                    {"user_id": user_id},
                ).scalar_one()
            )
            profile_id = session.execute(
                text(
                    """
                    INSERT INTO mt5_account_profiles (
                        user_id, broker, platform, account_environment, login, server,
                        metaapi_account_id, metaapi_token_ciphertext, metaapi_token_fingerprint,
                        status, remote_state, remote_connection_status, last_error_code,
                        last_checked_at, last_connected_at, is_active, updated_at
                    ) VALUES (
                        :user_id, 'vantage', 'mt5', :environment, :login, :server,
                        :metaapi_account_id, :ciphertext, :fingerprint,
                        :status, :remote_state, :remote_connection_status, NULL,
                        :now, :connected_at, FALSE, :now
                    )
                    ON CONFLICT (user_id, account_environment)
                    DO UPDATE SET
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
                        last_connected_at=COALESCE(EXCLUDED.last_connected_at, mt5_account_profiles.last_connected_at),
                        updated_at=EXCLUDED.updated_at
                    RETURNING id
                    """
                ),
                {
                    "user_id": user_id,
                    "environment": environment,
                    "login": normalized_login,
                    "server": normalized_server,
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
                    event_type="mt5.account_profile_connected",
                    entity_type="mt5_account_profile",
                    entity_id=profile_id,
                    payload={
                        "account_environment": environment,
                        "login_last4": normalized_login[-4:],
                        "server": normalized_server,
                        "status": local_status,
                        "password_stored": False,
                        "made_active": bool(make_active or not existing_active),
                    },
                )
            )
            session.commit()

        if make_active or not existing_active:
            self.activate_profile(user_id=user_id, role=role, environment=environment)
        return self.list_profiles(user_id)

    def activate_profile(self, *, user_id: UUID, role: str, environment: str) -> dict[str, Any]:
        normalized_environment = environment.strip().lower()
        if normalized_environment not in {"demo", "live"}:
            raise Mt5ConnectionError("mt5_environment_invalid")

        with self._session_factory() as session:
            target = session.execute(
                text(
                    """
                    SELECT * FROM mt5_account_profiles
                    WHERE user_id=:user_id
                      AND account_environment=:environment
                      AND status<>'revoked'
                    FOR UPDATE
                    """
                ),
                {"user_id": user_id, "environment": normalized_environment},
            ).mappings().first()
            if target is None:
                raise Mt5ConnectionError("mt5_profile_not_configured")
            if str(target["status"]) != "connected":
                raise Mt5ConnectionError("mt5_profile_not_connected")
            if bool(target["is_active"]):
                return self.list_profiles(user_id)

            exposure = int(
                session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM positions
                        WHERE user_id=:user_id AND status IN ('open','pending')
                        """
                    ),
                    {"user_id": user_id},
                ).scalar_one()
            )
            if exposure:
                raise Mt5ConnectionError("mt5_switch_open_exposure")

            if normalized_environment == "live" and role == "user":
                approved = session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM mt5_account_approvals
                            WHERE user_id=:user_id AND status='active'
                              AND login=:login AND lower(server)=lower(:server)
                        )
                        """
                    ),
                    {
                        "user_id": user_id,
                        "login": target["login"],
                        "server": target["server"],
                    },
                ).scalar_one()
                if not approved:
                    raise Mt5ConnectionError("mt5_account_not_approved")

            canonical = session.execute(
                text("SELECT id FROM mt5_accounts WHERE owner_user_id=:user_id FOR UPDATE"),
                {"user_id": user_id},
            ).scalar_one_or_none()

            if canonical is None:
                canonical = session.execute(
                    text(
                        """
                        INSERT INTO mt5_accounts (
                            owner_user_id, broker, platform, account_environment,
                            login, server, metaapi_account_id,
                            metaapi_token_ciphertext, metaapi_token_fingerprint,
                            status, remote_state, remote_connection_status,
                            last_error_code, last_checked_at, last_connected_at, updated_at
                        ) VALUES (
                            :user_id, :broker, :platform, :environment,
                            :login, :server, :metaapi_account_id,
                            :ciphertext, :fingerprint,
                            :status, :remote_state, :remote_connection_status,
                            :last_error_code, :last_checked_at, :last_connected_at, now()
                        ) RETURNING id
                        """
                    ),
                    {
                        "user_id": user_id,
                        "broker": target["broker"],
                        "platform": target["platform"],
                        "environment": target["account_environment"],
                        "login": target["login"],
                        "server": target["server"],
                        "metaapi_account_id": target["metaapi_account_id"],
                        "ciphertext": target["metaapi_token_ciphertext"],
                        "fingerprint": target["metaapi_token_fingerprint"],
                        "status": target["status"],
                        "remote_state": target["remote_state"],
                        "remote_connection_status": target["remote_connection_status"],
                        "last_error_code": target["last_error_code"],
                        "last_checked_at": target["last_checked_at"],
                        "last_connected_at": target["last_connected_at"],
                    },
                ).scalar_one()
            else:
                session.execute(
                    text(
                        """
                        UPDATE mt5_accounts SET
                            broker=:broker,
                            platform=:platform,
                            account_environment=:environment,
                            login=:login,
                            server=:server,
                            metaapi_account_id=:metaapi_account_id,
                            metaapi_token_ciphertext=:ciphertext,
                            metaapi_token_fingerprint=:fingerprint,
                            status=:status,
                            remote_state=:remote_state,
                            remote_connection_status=:remote_connection_status,
                            last_error_code=:last_error_code,
                            last_checked_at=:last_checked_at,
                            last_connected_at=:last_connected_at,
                            updated_at=now()
                        WHERE id=:id
                        """
                    ),
                    {
                        "id": canonical,
                        "broker": target["broker"],
                        "platform": target["platform"],
                        "environment": target["account_environment"],
                        "login": target["login"],
                        "server": target["server"],
                        "metaapi_account_id": target["metaapi_account_id"],
                        "ciphertext": target["metaapi_token_ciphertext"],
                        "fingerprint": target["metaapi_token_fingerprint"],
                        "status": target["status"],
                        "remote_state": target["remote_state"],
                        "remote_connection_status": target["remote_connection_status"],
                        "last_error_code": target["last_error_code"],
                        "last_checked_at": target["last_checked_at"],
                        "last_connected_at": target["last_connected_at"],
                    },
                )

            session.execute(
                text("UPDATE mt5_account_profiles SET is_active=FALSE, updated_at=now() WHERE user_id=:user_id"),
                {"user_id": user_id},
            )
            session.execute(
                text("UPDATE mt5_account_profiles SET is_active=TRUE, updated_at=now() WHERE id=:id"),
                {"id": target["id"]},
            )
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.active_account_switched",
                    entity_type="mt5_account",
                    entity_id=canonical,
                    payload={
                        "account_environment": normalized_environment,
                        "login_last4": str(target["login"])[-4:],
                        "server": str(target["server"]),
                        "open_or_pending_positions_at_switch": 0,
                        "real_money_active": normalized_environment == "live",
                    },
                )
            )
            session.commit()

        return self.list_profiles(user_id)
