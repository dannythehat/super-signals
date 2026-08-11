"""Explicit, auditable Day 22 MT5 reconciliation path.

This keeps the existing Day 22 provisioning surface but replaces the silent
per-account reconciliation failure path with a small implementation that records
safe diagnostic stages and writes successful MetaAPI connection state directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.models import AuditEvent
from app.mt5_connection_service import Mt5ConnectionView, Mt5DemoConnectionService
from app.mt5_crypto import BrokerCredentialDecryptionError


class Day22Mt5DemoConnectionService(Mt5DemoConnectionService):
    """Day 22 service with deterministic, auditable restart reconciliation."""

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
        status = (
            "disconnected"
            if error_code
            in {
                "metaapi_timeout",
                "metaapi_unreachable",
                "metaapi_temporarily_unavailable",
            }
            else "error"
        )
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE mt5_accounts
                    SET status = :status,
                        last_error_code = :error_code,
                        last_checked_at = :checked_at,
                        updated_at = :checked_at
                    WHERE id = :id
                    """
                ),
                {
                    "id": local_account_id,
                    "status": status,
                    "error_code": error_code,
                    "checked_at": now,
                },
            )
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
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
