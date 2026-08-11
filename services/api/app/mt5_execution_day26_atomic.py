"""Failure-atomic wrapper around the accepted Day 26 multi-TP executor.

The underlying Day26Mt5ExecutionService owns the normal three-TP execution path.
This wrapper adds one safety property: if that path fails after one or more orders
may have reached the demo broker, every position bearing the planned Super Signals
client IDs is closed immediately before the original error is allowed to escape.

This is compensation for a failed Day 26 transaction, not provider-driven trade
management. Normal close/modify semantics remain Day 27 scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    Day26Mt5ExecutionService,
    _AccountInput,
    _SignalInput,
)
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError


@dataclass(frozen=True, slots=True)
class Day26RollbackResult:
    attempted: bool
    submitted_count: int
    identified_count: int
    closed_count: int
    unresolved_count: int

    @property
    def complete(self) -> bool:
        return self.unresolved_count == 0


class AtomicDay26Mt5ExecutionService(Day26Mt5ExecutionService):
    """Day 26 execution with broker compensation on partial failure."""

    def _load_inputs(
        self, owner_user_id: UUID, signal_id: UUID
    ) -> tuple[_SignalInput, _AccountInput]:
        signal, account = super()._load_inputs(owner_user_id, signal_id)
        if len(signal.take_profits) != 3:
            raise Day26ExecutionError("day26_three_tps_required")
        return signal, account

    async def execute_owner_demo_signal(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        risk_percent,
        double_lot_approved: bool,
    ):
        try:
            return await super().execute_owner_demo_signal(
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )
        except Day26ExecutionError as original:
            rollback = await self._compensate_partial_execution(
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                original_code=original.code,
            )
            if rollback.attempted and not rollback.complete:
                raise Day26ExecutionError("day26_partial_execution_rollback_failed") from original
            raise

    async def _compensate_partial_execution(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        original_code: str,
    ) -> Day26RollbackResult:
        local_rows = self._rollback_rows(owner_user_id, signal_id)
        if not local_rows:
            return Day26RollbackResult(False, 0, 0, 0, 0)

        submitted = [row for row in local_rows if str(row.get("broker_order_id") or "").strip()]
        if not submitted:
            self._mark_unsubmitted_failed(local_rows, original_code)
            return Day26RollbackResult(False, 0, 0, 0, 0)

        attempted = True
        try:
            account_id, token = self._rollback_account(owner_user_id)
            day23 = Day23Mt5ReadService(
                session_factory=self._session_factory,
                cipher=self._cipher,
                gateway=self._read_gateway,
            )
            live_state = await day23.read_owner_live_state(owner_user_id)
            broker_positions = await self._read_gateway.read_positions(
                token=token,
                account_id=account_id,
                region=live_state.region,
            )
        except (Day23ReadError, MetaApiGatewayError, Day26ExecutionError):
            self._mark_rollback_unresolved(local_rows, original_code)
            self._audit_rollback(
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                original_code=original_code,
                submitted_count=len(submitted),
                identified_count=0,
                closed_count=0,
                unresolved_count=len(submitted),
            )
            return Day26RollbackResult(attempted, len(submitted), 0, 0, len(submitted))

        by_client_id = {
            str(row.get("clientId") or "").strip(): row
            for row in broker_positions
            if str(row.get("clientId") or "").strip()
        }
        identified: dict[UUID, str] = {}
        for row in submitted:
            local_id = row["id"]
            existing_position_id = str(row.get("broker_position_id") or "").strip()
            broker = by_client_id.get(str(row.get("broker_client_id") or ""))
            broker_position_id = existing_position_id or str(
                (broker or {}).get("id") or ""
            ).strip()
            if broker_position_id:
                identified[local_id] = broker_position_id

        closed: dict[UUID, str] = {}
        for row in reversed(submitted):
            local_id = row["id"]
            broker_position_id = identified.get(local_id)
            if not broker_position_id:
                continue
            try:
                await self._trade_gateway.close_position(
                    token=token,
                    account_id=account_id,
                    region=live_state.region,
                    position_id=broker_position_id,
                )
            except MetaApiGatewayError:
                continue
            closed[local_id] = broker_position_id

        unresolved = len(submitted) - len(closed)
        self._persist_rollback_state(
            local_rows=local_rows,
            identified=identified,
            closed=closed,
            original_code=original_code,
        )
        self._audit_rollback(
            owner_user_id=owner_user_id,
            signal_id=signal_id,
            original_code=original_code,
            submitted_count=len(submitted),
            identified_count=len(identified),
            closed_count=len(closed),
            unresolved_count=unresolved,
        )
        return Day26RollbackResult(
            attempted=attempted,
            submitted_count=len(submitted),
            identified_count=len(identified),
            closed_count=len(closed),
            unresolved_count=unresolved,
        )

    def _rollback_rows(self, owner_user_id: UUID, signal_id: UUID) -> list[dict]:
        with self._session_factory() as session:
            return [
                dict(row)
                for row in session.execute(
                    text(
                        """
                        SELECT id, broker_client_id, broker_order_id,
                               broker_position_id, status
                        FROM positions
                        WHERE signal_id = :signal_id AND user_id = :user_id
                        ORDER BY tp_index
                        """
                    ),
                    {"signal_id": signal_id, "user_id": owner_user_id},
                ).mappings().all()
            ]

    def _rollback_account(self, owner_user_id: UUID) -> tuple[str, str]:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id, metaapi_token_ciphertext,
                           account_environment, status
                    FROM mt5_accounts
                    WHERE owner_user_id = :owner_user_id
                      AND status != 'revoked'
                    LIMIT 1
                    """
                ),
                {"owner_user_id": owner_user_id},
            ).mappings().first()
        if row is None:
            raise Day26ExecutionError("mt5_account_not_configured")
        if str(row["account_environment"]).lower() != "demo":
            raise Day26ExecutionError("day26_demo_account_required")
        try:
            token = self._cipher.decrypt(bytes(row["metaapi_token_ciphertext"])).strip()
        except BrokerCredentialDecryptionError as exc:
            raise Day26ExecutionError("broker_credential_decryption_failed") from exc
        if len(token) < 20:
            raise Day26ExecutionError("metaapi_platform_token_not_configured")
        return str(row["metaapi_account_id"]), token

    def _mark_unsubmitted_failed(self, local_rows: list[dict], original_code: str) -> None:
        with self._session_factory() as session:
            for row in local_rows:
                session.execute(
                    text(
                        """
                        UPDATE positions
                        SET status = 'error', close_reason = :reason, updated_at = now()
                        WHERE id = :id
                        """
                    ),
                    {"id": row["id"], "reason": f"day26_failed:{original_code}"[:80]},
                )
            session.commit()

    def _mark_rollback_unresolved(self, local_rows: list[dict], original_code: str) -> None:
        with self._session_factory() as session:
            for row in local_rows:
                submitted = bool(str(row.get("broker_order_id") or "").strip())
                session.execute(
                    text(
                        """
                        UPDATE positions
                        SET status = :status, close_reason = :reason, updated_at = now()
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": row["id"],
                        "status": "error" if submitted else "error",
                        "reason": (
                            f"day26_rollback_unresolved:{original_code}"
                            if submitted
                            else f"day26_failed:{original_code}"
                        )[:80],
                    },
                )
            session.commit()

    def _persist_rollback_state(
        self,
        *,
        local_rows: list[dict],
        identified: dict[UUID, str],
        closed: dict[UUID, str],
        original_code: str,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            for row in local_rows:
                local_id = row["id"]
                submitted = bool(str(row.get("broker_order_id") or "").strip())
                broker_position_id = identified.get(local_id)
                if local_id in closed:
                    status = "closed"
                    reason = "day26_compensating_rollback"
                    closed_at = now
                elif submitted:
                    status = "error"
                    reason = f"day26_rollback_unresolved:{original_code}"[:80]
                    closed_at = None
                else:
                    status = "error"
                    reason = f"day26_failed:{original_code}"[:80]
                    closed_at = None
                session.execute(
                    text(
                        """
                        UPDATE positions
                        SET broker_position_id = COALESCE(:broker_position_id, broker_position_id),
                            status = :status,
                            close_reason = :reason,
                            closed_at = :closed_at,
                            updated_at = :updated_at
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": local_id,
                        "broker_position_id": broker_position_id,
                        "status": status,
                        "reason": reason,
                        "closed_at": closed_at,
                        "updated_at": now,
                    },
                )
            session.commit()

    def _audit_rollback(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        original_code: str,
        submitted_count: int,
        identified_count: int,
        closed_count: int,
        unresolved_count: int,
    ) -> None:
        self._audit(
            owner_user_id=owner_user_id,
            signal_id=signal_id,
            event_type="mt5.day26_compensating_rollback",
            payload={
                "original_error_code": original_code,
                "submitted_order_count": submitted_count,
                "identified_position_count": identified_count,
                "closed_position_count": closed_count,
                "unresolved_position_count": unresolved_count,
                "rollback_complete": unresolved_count == 0,
                "automatic_trade_retry": False,
            },
        )
