"""Explicit, auditable Day 22 MT5 reconciliation path.

This keeps the existing Day 22 provisioning surface but replaces the silent
per-account reconciliation failure path with a small implementation that records
safe diagnostic stages and writes successful MetaAPI connection state directly.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.models import AuditEvent
from app.mt5_connection_service import (
    Mt5ConnectionError,
    Mt5ConnectionView,
    Mt5DemoConnectionService,
)
from app.mt5_crypto import BrokerCredentialDecryptionError

_TRANSIENT_RECONCILE_CODES = {
    "metaapi_timeout",
    "metaapi_unreachable",
    "metaapi_temporarily_unavailable",
}


class Day22Mt5DemoConnectionService(Mt5DemoConnectionService):
    """Day 22 service with deterministic, auditable restart reconciliation."""

    def resolve_platform_token(self) -> str:
        """Return the MetaAPI platform token without requiring repeated setup.

        A temporary Render environment token may bootstrap a brand-new deployment.
        Once any MT5 account has been registered, the encrypted token stored in
        PostgreSQL becomes the durable source. This means future reconnects and
        later user onboarding do not require the owner to paste or reconfigure the
        MetaAPI token again.
        """

        environment_token = os.getenv("SUPER_SIGNALS_API", "").strip()
        if len(environment_token) >= 20:
            return environment_token

        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_token_ciphertext
                    FROM mt5_accounts
                    WHERE status != 'revoked'
                    ORDER BY last_connected_at DESC NULLS LAST, created_at ASC
                    LIMIT 1
                    """
                )
            ).mappings().first()

        if row is None:
            raise Mt5ConnectionError("metaapi_platform_token_not_configured")

        try:
            token = self._cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
        except BrokerCredentialDecryptionError as exc:
            raise Mt5ConnectionError("broker_credential_decryption_failed") from exc

        if len(token.strip()) < 20:
            raise Mt5ConnectionError("metaapi_platform_token_not_configured")
        return token.strip()

    async def reconcile_all(self) -> int:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT *
                    FROM mt5_accounts
                    WHERE status != 'revoked'
                    ORDER BY created_at ASC
                    """
                )
            ).mappings().all()

        checked = 0
        for row in rows:
            await self._refresh_row_day22(row)
            checked += 1
        return checked

    async def refresh_owner_demo(self, owner_user_id: UUID) -> Mt5ConnectionView:
        row = self._load_row(owner_user_id)
        if row is None:
            return self._empty_view()
        await self._refresh_row_day22(row)
        return self.get_status(owner_user_id)

    async def _refresh_row_day22(self, row: Any) -> None:
        local_account_id = row["id"]
        stage = "decrypt"

        try:
            token = self._cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
        except BrokerCredentialDecryptionError:
            self._record_local_failure(
                local_account_id,
                stage=stage,
                error_code="broker_credential_decryption_failed",
                error_kind="BrokerCredentialDecryptionError",
            )
            return

        try:
            stage = "read_account"
            remote = await self._gateway.read_account(
                token=token,
                account_id=str(row["metaapi_account_id"]),
            )

            if remote.state in {
                "CREATED",
                "UNDEPLOYED",
                "DEPLOY_FAILED",
                "UNDEPLOY_FAILED",
            }:
                stage = "deploy_account"
                await self._gateway.deploy_account(
                    token=token,
                    account_id=remote.account_id,
                )
                stage = "read_after_deploy"
                remote = await self._gateway.read_account(
                    token=token,
                    account_id=remote.account_id,
                )

            stage = "write_state"
            self._write_remote_state(local_account_id, remote)
        except MetaApiGatewayError as exc:
            self._record_local_failure(
                local_account_id,
                stage=stage,
                error_code=exc.code,
                error_kind="MetaApiGatewayError",
                retryable=exc.retryable,
            )
        except Exception as exc:
            self._record_local_failure(
                local_account_id,
                stage=stage,
                error_code="mt5_reconcile_internal_error",
                error_kind=type(exc).__name__,
            )

    def _write_remote_state(self, local_account_id: UUID, remote: Any) -> None:
        status = self._local_status(remote)
        now = datetime.now(UTC)
        with self._session_factory() as session:
            previous = session.execute(
                text("SELECT status FROM mt5_accounts WHERE id = :id FOR UPDATE"),
                {"id": local_account_id},
            ).scalar_one_or_none()

            session.execute(
                text(
                    """
                    UPDATE mt5_accounts
                    SET status = :new_status,
                        remote_state = :remote_state,
                        remote_connection_status = :remote_connection_status,
                        last_error_code = NULL,
                        last_checked_at = :checked_at,
                        last_connected_at = CASE
                            WHEN :is_connected THEN :checked_at
                            ELSE last_connected_at
                        END,
                        updated_at = :checked_at
                    WHERE id = :id
                    """
                ),
                {
                    "id": local_account_id,
                    "new_status": status,
                    "remote_state": remote.state,
                    "remote_connection_status": remote.connection_status,
                    "is_connected": status == "connected",
                    "checked_at": now,
                },
            )
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.day22_reconcile_success",
                    entity_type="mt5_account",
                    entity_id=local_account_id,
                    payload={
                        "from": previous,
                        "to": status,
                        "remote_state": remote.state,
                        "remote_connection_status": remote.connection_status,
                        "metaapi_read_completed": True,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _record_local_failure(
        self,
        local_account_id: UUID,
        *,
        stage: str,
        error_code: str,
        error_kind: str,
        retryable: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        transient = error_code in _TRANSIENT_RECONCILE_CODES
        with self._session_factory() as session:
            previous = session.execute(
                text("SELECT status FROM mt5_accounts WHERE id = :id FOR UPDATE"),
                {"id": local_account_id},
            ).scalar_one_or_none()

            # A background provisioning/API timeout is not proof that the terminal is
            # disconnected.  Downgrading a previously connected account here poisoned
            # the cached status and could block valid signals until the next hourly
            # reconciliation even when MT5 recovered seconds later.  Preserve the
            # last known connection status for transient transport failures; the live
            # execution read still has to succeed before any broker order is sent.
            if transient:
                session.execute(
                    text(
                        """
                        UPDATE mt5_accounts
                        SET last_error_code = :error_code,
                            last_checked_at = :checked_at,
                            updated_at = :checked_at
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": local_account_id,
                        "error_code": error_code,
                        "checked_at": now,
                    },
                )
                resulting_status = previous
            else:
                session.execute(
                    text(
                        """
                        UPDATE mt5_accounts
                        SET status = 'error',
                            last_error_code = :error_code,
                            last_checked_at = :checked_at,
                            updated_at = :checked_at
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": local_account_id,
                        "error_code": error_code,
                        "checked_at": now,
                    },
                )
                resulting_status = "error"

            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.day22_reconcile_failure",
                    entity_type="mt5_account",
                    entity_id=local_account_id,
                    payload={
                        "stage": stage,
                        "error_code": error_code,
                        "error_kind": error_kind,
                        "retryable": retryable,
                        "status_preserved": transient,
                        "status_before": previous,
                        "status_after": resulting_status,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
