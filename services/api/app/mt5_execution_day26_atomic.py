"""Failure-atomic wrapper around the Day 26 multi-position executor.

If execution fails after one or more broker mutations may have occurred, every planned
Super Signals client ID is reconciled against current MT5 positions/orders and immutable
broker history before the original error is allowed to escape. Exact artifacts are
compensated by exact broker ID. Ambiguous MetaAPI mutation failures are never blindly
retried.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.mt5_execution_day26 import Day26ExecutionError, Day26Mt5ExecutionService
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError

_AMBIGUOUS_BROKER_CODES = {
    "metaapi_timeout",
    "metaapi_unreachable",
    "metaapi_temporarily_unavailable",
}
_RECONCILE_ATTEMPTS = 3
_RECONCILE_DELAY_SECONDS = 0.35
_TERMINAL_NO_FILL_STATES = {
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
}
_FILLED_STATES = {"ORDER_STATE_FILLED", "ORDER_STATE_PARTIAL"}


@dataclass(frozen=True, slots=True)
class Day26RollbackResult:
    attempted: bool
    submitted_count: int
    identified_count: int
    closed_count: int
    unresolved_count: int
    hidden_mutation_count: int = 0

    @property
    def complete(self) -> bool:
        return self.unresolved_count == 0


class AtomicDay26Mt5ExecutionService(Day26Mt5ExecutionService):
    """Day 26 execution with broker-authoritative compensation on partial failure."""

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
                raise Day26ExecutionError(
                    "day26_partial_execution_rollback_failed"
                ) from original
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
            return Day26RollbackResult(False, 0, 0, 0, 0, 0)

        ambiguous = original_code in _AMBIGUOUS_BROKER_CODES
        submitted = [
            row for row in local_rows if str(row.get("broker_order_id") or "").strip()
        ]
        if not submitted and not ambiguous:
            self._mark_unsubmitted_failed(local_rows, original_code)
            return Day26RollbackResult(False, 0, 0, 0, 0, 0)

        attempted = True
        account_id: str
        token: str
        live_state = None
        broker_positions: list[dict[str, object]] = []
        broker_orders: list[dict[str, object]] = []
        read_failed = True
        attempts = _RECONCILE_ATTEMPTS if ambiguous else 1

        try:
            account_id, token = self._rollback_account(owner_user_id)
        except Day26ExecutionError:
            unresolved_rows = local_rows if ambiguous else submitted
            self._mark_rollback_unresolved(local_rows, original_code)
            self._audit_rollback(
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                original_code=original_code,
                submitted_count=len(submitted),
                identified_count=0,
                closed_count=0,
                unresolved_count=len(unresolved_rows),
                hidden_mutation_count=0,
                history_mutation_count=0,
            )
            return Day26RollbackResult(
                attempted, len(submitted), 0, 0, len(unresolved_rows), 0
            )

        for attempt in range(attempts):
            try:
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
                broker_orders = await self._read_gateway.read_orders(
                    token=token,
                    account_id=account_id,
                    region=live_state.region,
                )
                read_failed = False
                break
            except (Day23ReadError, MetaApiGatewayError):
                if attempt + 1 < attempts:
                    await asyncio.sleep(_RECONCILE_DELAY_SECONDS * (attempt + 1))

        if read_failed or live_state is None:
            unresolved_rows = local_rows if ambiguous else submitted
            self._mark_rollback_unresolved(local_rows, original_code)
            self._audit_rollback(
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                original_code=original_code,
                submitted_count=len(submitted),
                identified_count=0,
                closed_count=0,
                unresolved_count=len(unresolved_rows),
                hidden_mutation_count=0,
                history_mutation_count=0,
            )
            return Day26RollbackResult(
                attempted, len(submitted), 0, 0, len(unresolved_rows), 0
            )

        by_client_position = {
            str(row.get("clientId") or "").strip(): row
            for row in broker_positions
            if str(row.get("clientId") or "").strip()
        }
        by_client_order = {
            str(row.get("clientId") or "").strip(): row
            for row in broker_orders
            if str(row.get("clientId") or "").strip()
        }
        active_order_ids = {
            str(row.get("id") or "").strip()
            for row in broker_orders
            if str(row.get("id") or "").strip()
        }

        identified: dict[UUID, str] = {}
        discovered_order_ids: dict[UUID, str] = {}
        hidden_mutations: set[UUID] = set()
        for row in local_rows:
            local_id = row["id"]
            client_id = str(row.get("broker_client_id") or "").strip()
            current_position = by_client_position.get(client_id)
            current_order = by_client_order.get(client_id)
            existing_position_id = str(row.get("broker_position_id") or "").strip()
            position_id = existing_position_id or str(
                (current_position or {}).get("id") or ""
            ).strip()
            if position_id:
                identified[local_id] = position_id
            order_id = str(row.get("broker_order_id") or "").strip() or str(
                (current_order or {}).get("id") or ""
            ).strip()
            if order_id:
                discovered_order_ids[local_id] = order_id
            if not str(row.get("broker_order_id") or "").strip() and (
                current_position is not None or current_order is not None
            ):
                hidden_mutations.add(local_id)

        closed: dict[UUID, str] = {}
        cancelled: set[UUID] = set()
        no_fill_history: set[UUID] = set()
        history_mutations: set[UUID] = set()
        unresolved: set[UUID] = set()

        for row in reversed(local_rows):
            local_id = row["id"]
            broker_position_id = identified.get(local_id)
            order_id = discovered_order_ids.get(local_id, "")

            if broker_position_id:
                try:
                    await self._trade_gateway.close_position(
                        token=token,
                        account_id=account_id,
                        region=live_state.region,
                        position_id=broker_position_id,
                    )
                except MetaApiGatewayError:
                    unresolved.add(local_id)
                else:
                    closed[local_id] = broker_position_id
                continue

            if order_id and order_id in active_order_ids:
                try:
                    await self._trade_gateway.cancel_order(
                        token=token,
                        account_id=account_id,
                        region=live_state.region,
                        order_id=order_id,
                    )
                except MetaApiGatewayError:
                    unresolved.add(local_id)
                else:
                    cancelled.add(local_id)
                continue

            if order_id:
                state, history_position_id = await self._history_ticket_state(
                    token=token,
                    account_id=account_id,
                    region=live_state.region,
                    order_id=order_id,
                    expected_client_id=str(row.get("broker_client_id") or "").strip(),
                )
                if state in _TERMINAL_NO_FILL_STATES:
                    no_fill_history.add(local_id)
                    continue
                if state in _FILLED_STATES or history_position_id:
                    history_mutations.add(local_id)
                    unresolved.add(local_id)
                    continue
                # A returned broker order id that is in neither active state nor
                # immutable history is unresolved, not evidence that nothing happened.
                if local_id in {item["id"] for item in submitted}:
                    unresolved.add(local_id)
                    continue

            if ambiguous and local_id not in hidden_mutations:
                history_seen = await self._history_contains_client_id(
                    token=token,
                    account_id=account_id,
                    region=live_state.region,
                    client_id=str(row.get("broker_client_id") or "").strip(),
                    created_at=row.get("created_at"),
                )
                if history_seen:
                    history_mutations.add(local_id)
                    unresolved.add(local_id)

        self._persist_rollback_state(
            local_rows=local_rows,
            identified=identified,
            closed=closed,
            cancelled=cancelled,
            no_fill_history=no_fill_history,
            hidden_mutations=hidden_mutations,
            history_mutations=history_mutations,
            unresolved=unresolved,
            original_code=original_code,
        )
        self._audit_rollback(
            owner_user_id=owner_user_id,
            signal_id=signal_id,
            original_code=original_code,
            submitted_count=len(submitted),
            identified_count=len(identified),
            closed_count=len(closed) + len(cancelled),
            unresolved_count=len(unresolved),
            hidden_mutation_count=len(hidden_mutations),
            history_mutation_count=len(history_mutations),
        )
        return Day26RollbackResult(
            attempted=attempted,
            submitted_count=len(submitted),
            identified_count=len(identified),
            closed_count=len(closed) + len(cancelled),
            unresolved_count=len(unresolved),
            hidden_mutation_count=len(hidden_mutations),
        )

    async def _history_ticket_state(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        order_id: str,
        expected_client_id: str,
    ) -> tuple[str | None, str | None]:
        try:
            history = await self._read_gateway.read_history_orders_by_ticket(
                token=token,
                account_id=account_id,
                region=region,
                order_id=order_id,
            )
        except MetaApiGatewayError:
            return None, None
        matches = [
            item
            for item in history
            if str(item.get("id") or "").strip() == order_id
            and (
                not expected_client_id
                or not str(item.get("clientId") or "").strip()
                or str(item.get("clientId") or "").strip() == expected_client_id
            )
        ]
        if len(matches) != 1:
            return None, None
        item = matches[0]
        return (
            str(item.get("state") or "").strip().upper() or None,
            str(item.get("positionId") or "").strip() or None,
        )

    async def _history_contains_client_id(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        client_id: str,
        created_at,
    ) -> bool:
        if not client_id:
            return False
        now = datetime.now(UTC)
        start = (
            created_at if isinstance(created_at, datetime) else now - timedelta(minutes=10)
        )
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        start = start.astimezone(UTC) - timedelta(minutes=2)
        end = now + timedelta(minutes=1)
        try:
            orders = await self._read_gateway.read_history_orders_by_time_range(
                token=token,
                account_id=account_id,
                region=region,
                start_time=start,
                end_time=end,
            )
            if any(str(item.get("clientId") or "").strip() == client_id for item in orders):
                return True
            deals = await self._read_gateway.read_deals_by_time_range(
                token=token,
                account_id=account_id,
                region=region,
                start_time=start,
                end_time=end,
            )
            return any(str(item.get("clientId") or "").strip() == client_id for item in deals)
        except MetaApiGatewayError:
            # History unavailable means the ambiguous mutation is not proven safe.
            return True

    def _rollback_rows(
        self,
        owner_user_id: UUID,
        signal_id: UUID,
    ) -> list[dict]:
        with self._session_factory() as session:
            return [
                dict(row)
                for row in session.execute(
                    text(
                        """
                        SELECT id, broker_client_id, broker_order_id,
                               broker_position_id, status, created_at
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
            token = self._cipher.decrypt(
                bytes(row["metaapi_token_ciphertext"])
            ).strip()
        except BrokerCredentialDecryptionError as exc:
            raise Day26ExecutionError("broker_credential_decryption_failed") from exc
        if len(token) < 20:
            raise Day26ExecutionError("metaapi_platform_token_not_configured")
        return str(row["metaapi_account_id"]), token

    def _mark_unsubmitted_failed(
        self,
        local_rows: list[dict],
        original_code: str,
    ) -> None:
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
                    {
                        "id": row["id"],
                        "reason": f"day26_failed:{original_code}"[:80],
                    },
                )
            session.commit()

    def _mark_rollback_unresolved(
        self,
        local_rows: list[dict],
        original_code: str,
    ) -> None:
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
                    {
                        "id": row["id"],
                        "reason": f"day26_rollback_unresolved:{original_code}"[:80],
                    },
                )
            session.commit()

    def _persist_rollback_state(
        self,
        *,
        local_rows: list[dict],
        identified: dict[UUID, str],
        closed: dict[UUID, str],
        cancelled: set[UUID],
        no_fill_history: set[UUID],
        hidden_mutations: set[UUID],
        history_mutations: set[UUID],
        unresolved: set[UUID],
        original_code: str,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            for row in local_rows:
                local_id = row["id"]
                broker_position_id = identified.get(local_id)
                if local_id in closed or local_id in cancelled:
                    status = "closed"
                    reason = "day26_compensating_rollback"
                    closed_at = now
                elif local_id in unresolved or local_id in history_mutations:
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
                        SET broker_position_id = COALESCE(
                                :broker_position_id, broker_position_id
                            ),
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
        hidden_mutation_count: int,
        history_mutation_count: int,
    ) -> None:
        self._audit(
            owner_user_id=owner_user_id,
            signal_id=signal_id,
            event_type="mt5.day26_compensating_rollback",
            payload={
                "original_error_code": original_code,
                "submitted_order_count": submitted_count,
                "identified_position_count": identified_count,
                "compensated_broker_artifact_count": closed_count,
                "hidden_mutation_count": hidden_mutation_count,
                "history_mutation_count": history_mutation_count,
                "unresolved_position_count": unresolved_count,
                "rollback_complete": unresolved_count == 0,
                "automatic_trade_retry": False,
            },
        )