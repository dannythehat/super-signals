"""Day 27 Vantage demo follow-up execution.

This service consumes one already-linked provider lifecycle event and applies only its
mechanically explicit management actions to mapped positions that still exist at the
broker. Broker state is authoritative before every mutation. Missing mapped positions
are reconciled closed and are never reopened as compensation or as a retry strategy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher


class Day27ManagementError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Day27ManagementResult:
    lifecycle_event_id: UUID
    signal_id: UUID
    user_id: UUID
    actions_requested: int
    broker_actions_sent: int
    positions_closed: int
    positions_modified: int
    orders_cancelled: int
    external_positions_reconciled: int
    already_applied: bool = False


@dataclass(frozen=True, slots=True)
class _Account:
    local_id: UUID
    account_id: str
    token_ciphertext: bytes


@dataclass(frozen=True, slots=True)
class _LocalPosition:
    id: UUID
    tp_index: int
    broker_position_id: str | None
    broker_order_id: str | None
    status: str
    stop_loss: Decimal | None
    take_profit: Decimal | None


class Day27Mt5ManagementService:
    """Apply a canonical provider lifecycle event to the connected Vantage demo."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        read_gateway: MetaApiReadGateway,
        trade_gateway: MetaApiTradeGateway,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._read = read_gateway
        self._trade = trade_gateway

    async def execute_owner_demo_event(
        self,
        *,
        owner_user_id: UUID,
        lifecycle_event_id: UUID,
    ) -> Day27ManagementResult:
        existing = self._existing_success(owner_user_id, lifecycle_event_id)
        if existing is not None:
            return existing

        event = self._load_event(lifecycle_event_id)
        if event is None:
            raise Day27ManagementError("day27_lifecycle_event_not_found")
        actions = self._actions(event)
        if not actions:
            raise Day27ManagementError("day27_management_action_missing")

        account = self._load_account(owner_user_id)
        if account is None:
            raise Day27ManagementError("mt5_account_not_configured")
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
        except BrokerCredentialDecryptionError as exc:
            raise Day27ManagementError("broker_credential_decryption_failed") from exc

        try:
            region = await self._read.resolve_account_region(
                token=token,
                account_id=account.account_id,
            )
        except MetaApiGatewayError as exc:
            raise Day27ManagementError(exc.code, retryable=exc.retryable) from exc

        signal_id = UUID(str(event["signal_id"]))
        counters = {
            "broker_actions_sent": 0,
            "positions_closed": 0,
            "positions_modified": 0,
            "orders_cancelled": 0,
            "external_positions_reconciled": 0,
        }

        try:
            for action in actions:
                # Re-read before every action. A provider message may contain multiple
                # instructions and an earlier close changes the eligible remainder.
                broker_positions = await self._broker_positions(
                    token=token, account_id=account.account_id, region=region
                )
                counters["external_positions_reconciled"] += self._reconcile_missing_positions(
                    signal_id=signal_id,
                    user_id=owner_user_id,
                    broker_position_ids=set(broker_positions),
                )
                local_positions = self._load_positions(signal_id, owner_user_id)
                open_positions = tuple(
                    item
                    for item in local_positions
                    if item.status == "open"
                    and item.broker_position_id is not None
                    and item.broker_position_id in broker_positions
                )

                action_type = str(action.get("type") or "")
                target = str(action.get("target") or "all")
                value = self._positive_decimal(action.get("value"))

                if action_type == "close":
                    selected = self._select_positions(open_positions, target)
                    for item in selected:
                        assert item.broker_position_id is not None
                        await self._trade.close_position(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            position_id=item.broker_position_id,
                        )
                        counters["broker_actions_sent"] += 1
                        counters["positions_closed"] += 1
                        self._mark_provider_closed(item.id)
                    continue

                if action_type in {"move_to_break_even", "edit_stop_loss"}:
                    selected = self._select_positions(open_positions, target)
                    for item in selected:
                        assert item.broker_position_id is not None
                        broker = broker_positions[item.broker_position_id]
                        desired_sl = (
                            self._positive_decimal(broker.get("openPrice"))
                            if action_type == "move_to_break_even"
                            else value
                        )
                        if desired_sl is None:
                            raise Day27ManagementError("day27_stop_loss_value_invalid")
                        current_sl = self._positive_decimal(broker.get("stopLoss"))
                        if self._same_price(current_sl, desired_sl):
                            self._update_local_stop(item.id, desired_sl)
                            continue
                        await self._trade.modify_position(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            position_id=item.broker_position_id,
                            stop_loss=float(desired_sl),
                        )
                        counters["broker_actions_sent"] += 1
                        counters["positions_modified"] += 1
                        self._update_local_stop(item.id, desired_sl)
                    continue

                if action_type == "edit_take_profit":
                    if value is None:
                        raise Day27ManagementError("day27_take_profit_value_invalid")
                    selected = self._select_positions(open_positions, target)
                    for item in selected:
                        assert item.broker_position_id is not None
                        broker = broker_positions[item.broker_position_id]
                        current_tp = self._positive_decimal(broker.get("takeProfit"))
                        if self._same_price(current_tp, value):
                            self._update_local_tp(item.id, value)
                            continue
                        await self._trade.modify_position(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            position_id=item.broker_position_id,
                            take_profit=float(value),
                        )
                        counters["broker_actions_sent"] += 1
                        counters["positions_modified"] += 1
                        self._update_local_tp(item.id, value)
                    continue

                if action_type == "cancel_pending":
                    broker_orders = await self._broker_orders(
                        token=token, account_id=account.account_id, region=region
                    )
                    mapped_orders = {
                        item.broker_order_id: item
                        for item in local_positions
                        if item.broker_order_id is not None
                    }
                    for order_id in sorted(set(mapped_orders).intersection(broker_orders)):
                        await self._trade.cancel_order(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            order_id=order_id,
                        )
                        if mapped_orders[order_id].status == "pending":
                            self._mark_pending_cancelled(
                                mapped_orders[order_id].id,
                                reason="provider_cancel_pending",
                            )
                        counters["broker_actions_sent"] += 1
                        counters["orders_cancelled"] += 1
                    continue

                raise Day27ManagementError("day27_management_action_unsupported")
        except MetaApiGatewayError as exc:
            self._audit_failure(
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
                signal_id=signal_id,
                actions=actions,
                counters=counters,
                error_code=exc.code,
            )
            raise Day27ManagementError(exc.code, retryable=exc.retryable) from exc
        except Day27ManagementError as exc:
            self._audit_failure(
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
                signal_id=signal_id,
                actions=actions,
                counters=counters,
                error_code=exc.code,
            )
            raise

        result = Day27ManagementResult(
            lifecycle_event_id=lifecycle_event_id,
            signal_id=signal_id,
            user_id=owner_user_id,
            actions_requested=len(actions),
            broker_actions_sent=counters["broker_actions_sent"],
            positions_closed=counters["positions_closed"],
            positions_modified=counters["positions_modified"],
            orders_cancelled=counters["orders_cancelled"],
            external_positions_reconciled=counters["external_positions_reconciled"],
        )
        self._audit_success(result, actions)
        return result

    def _load_event(self, event_id: UUID) -> Any | None:
        with self._session_factory() as session:
            return session.execute(
                text(
                    """
                    SELECT id, signal_id, event_type, aggregate_result
                    FROM signal_lifecycle_events
                    WHERE id = :event_id
                      AND origin = 'provider_update'
                    LIMIT 1
                    """
                ),
                {"event_id": event_id},
            ).mappings().first()

    @staticmethod
    def _actions(event: Any) -> tuple[dict[str, Any], ...]:
        aggregate = event["aggregate_result"] if isinstance(event["aggregate_result"], dict) else {}
        revised = aggregate.get("revised_instruction")
        if not isinstance(revised, dict):
            return ()
        raw_actions = revised.get("management_actions")
        if isinstance(raw_actions, list):
            actions = tuple(item for item in raw_actions if isinstance(item, dict))
            if actions:
                return actions
        update_type = revised.get("update_type")
        if update_type in {
            "close",
            "move_to_break_even",
            "edit_stop_loss",
            "edit_take_profit",
            "cancel_pending",
        }:
            return (
                {
                    "type": update_type,
                    "target": revised.get("update_target") or "all",
                    "value": revised.get("update_value"),
                },
            )
        return ()

    async def force_close_all_positions(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
    ) -> int:
        """Flatten every broker-confirmed open position for a signal, unconditionally.

        Only called after a provider management instruction repeatedly could not be
        resolved (see ``management_reliability_runtime``). It never parses provider text
        or a target token -- it is the same unambiguous "close everything" outcome every
        ordinary full close already reaches via ``_select_positions``' default branch,
        just invoked directly so a position is never left open and unprotected forever
        because one message could not be matched.
        """
        account = self._load_account(owner_user_id)
        if account is None:
            raise Day27ManagementError("mt5_account_not_configured")
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
        except BrokerCredentialDecryptionError as exc:
            raise Day27ManagementError("broker_credential_decryption_failed") from exc
        try:
            region = await self._read.resolve_account_region(
                token=token, account_id=account.account_id,
            )
        except MetaApiGatewayError as exc:
            raise Day27ManagementError(exc.code, retryable=exc.retryable) from exc

        broker_positions = await self._broker_positions(
            token=token, account_id=account.account_id, region=region
        )
        self._reconcile_missing_positions(
            signal_id=signal_id,
            user_id=owner_user_id,
            broker_position_ids=set(broker_positions),
        )
        local_positions = self._load_positions(signal_id, owner_user_id)
        open_positions = tuple(
            item
            for item in local_positions
            if item.status == "open"
            and item.broker_position_id is not None
            and item.broker_position_id in broker_positions
        )

        closed = 0
        for item in open_positions:
            assert item.broker_position_id is not None
            await self._trade.close_position(
                token=token,
                account_id=account.account_id,
                region=region,
                position_id=item.broker_position_id,
            )
            closed += 1
            self._mark_failsafe_closed(item.id)

        broker_orders = await self._broker_orders(
            token=token, account_id=account.account_id, region=region
        )
        pending_positions = tuple(
            item
            for item in self._load_positions(signal_id, owner_user_id)
            if item.status == "pending"
            and item.broker_order_id is not None
            and item.broker_order_id in broker_orders
        )
        for item in pending_positions:
            assert item.broker_order_id is not None
            await self._trade.cancel_order(
                token=token,
                account_id=account.account_id,
                region=region,
                order_id=item.broker_order_id,
            )
            self._mark_pending_cancelled(
                item.id,
                reason="management_failsafe_cancel_pending",
            )
        return closed

    def _mark_pending_cancelled(self, position_id: UUID, *, reason: str) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='cancelled',
                        close_reason=COALESCE(close_reason,:reason),
                        updated_at=now()
                    WHERE id=:position_id AND status='pending'
                    """
                ),
                {"position_id": position_id, "reason": reason},
            )
            session.commit()

    def _mark_failsafe_closed(self, position_id: UUID) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET status = 'closed', closed_at = COALESCE(closed_at, :now),
                        close_reason = 'management_failsafe_close', updated_at = :now
                    WHERE id = :position_id AND status = 'open'
                    """
                ),
                {"position_id": position_id, "now": now},
            )
            session.commit()

    def _load_account(self, owner_user_id: UUID) -> _Account | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, metaapi_account_id, metaapi_token_ciphertext,
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
            return None
        if str(row["account_environment"]) != "demo":
            raise Day27ManagementError("day27_demo_account_required")
        if str(row["status"]) != "connected":
            raise Day27ManagementError("mt5_account_not_connected", retryable=True)
        return _Account(
            local_id=row["id"],
            account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )

    def _load_positions(self, signal_id: UUID, user_id: UUID) -> tuple[_LocalPosition, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, tp_index, broker_position_id, broker_order_id, status,
                           stop_loss, take_profit
                    FROM positions
                    WHERE signal_id = :signal_id AND user_id = :user_id
                    ORDER BY tp_index
                    """
                ),
                {"signal_id": signal_id, "user_id": user_id},
            ).mappings().all()
        return tuple(
            _LocalPosition(
                id=row["id"],
                tp_index=int(row["tp_index"]),
                broker_position_id=(str(row["broker_position_id"]) if row["broker_position_id"] else None),
                broker_order_id=(str(row["broker_order_id"]) if row["broker_order_id"] else None),
                status=str(row["status"]),
                stop_loss=self._positive_decimal(row["stop_loss"]),
                take_profit=self._positive_decimal(row["take_profit"]),
            )
            for row in rows
        )

    async def _broker_positions(
        self, *, token: str, account_id: str, region: str
    ) -> dict[str, dict[str, object]]:
        payload = await self._read.read_positions(
            token=token, account_id=account_id, region=region
        )
        result: dict[str, dict[str, object]] = {}
        for item in payload:
            position_id = str(item.get("id") or "").strip()
            if position_id:
                result[position_id] = item
        return result

    async def _broker_orders(
        self, *, token: str, account_id: str, region: str
    ) -> set[str]:
        payload = await self._read.read_orders(
            token=token, account_id=account_id, region=region
        )
        return {str(item.get("id") or "").strip() for item in payload if str(item.get("id") or "").strip()}

    def _reconcile_missing_positions(
        self,
        *,
        signal_id: UUID,
        user_id: UUID,
        broker_position_ids: set[str],
    ) -> int:
        positions = self._load_positions(signal_id, user_id)
        missing = [
            item.id
            for item in positions
            if item.status == "open"
            and item.broker_position_id is not None
            and item.broker_position_id not in broker_position_ids
        ]
        if not missing:
            return 0
        now = datetime.now(UTC)
        with self._session_factory() as session:
            for position_id in missing:
                session.execute(
                    text(
                        """
                        UPDATE positions
                        SET status = 'closed',
                            closed_at = COALESCE(closed_at, :now),
                            close_reason = COALESCE(close_reason, 'external_close'),
                            updated_at = :now
                        WHERE id = :position_id AND status = 'open'
                        """
                    ),
                    {"position_id": position_id, "now": now},
                )
            session.commit()
        return len(missing)

    @staticmethod
    def _select_positions(
        positions: tuple[_LocalPosition, ...], target: str
    ) -> tuple[_LocalPosition, ...]:
        normalized = target.strip().lower()
        if normalized in {"", "all", "remaining", "rest", "entry_1", "entry1"}:
            return positions
        if normalized.startswith("tp") and normalized[2:].isdigit():
            index = int(normalized[2:])
            return tuple(item for item in positions if item.tp_index == index)
        if normalized.startswith("position"):
            digits = "".join(char for char in normalized if char.isdigit())
            if digits:
                index = int(digits)
                return tuple(item for item in positions if item.tp_index == index)
        raise Day27ManagementError("day27_management_target_unsupported")

    def _mark_provider_closed(self, position_id: UUID) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET status = 'closed', closed_at = COALESCE(closed_at, :now),
                        close_reason = COALESCE(close_reason, 'provider_close'),
                        updated_at = :now
                    WHERE id = :position_id AND status = 'open'
                    """
                ),
                {"position_id": position_id, "now": now},
            )
            session.commit()

    def _update_local_stop(self, position_id: UUID, value: Decimal) -> None:
        with self._session_factory() as session:
            session.execute(
                text("UPDATE positions SET stop_loss=:value, updated_at=now() WHERE id=:id"),
                {"id": position_id, "value": value},
            )
            session.commit()

    def _update_local_tp(self, position_id: UUID, value: Decimal) -> None:
        with self._session_factory() as session:
            session.execute(
                text("UPDATE positions SET take_profit=:value, updated_at=now() WHERE id=:id"),
                {"id": position_id, "value": value},
            )
            session.commit()

    @staticmethod
    def _positive_decimal(value: object) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return parsed if parsed.is_finite() and parsed > 0 else None

    @staticmethod
    def _same_price(left: Decimal | None, right: Decimal | None) -> bool:
        if left is None or right is None:
            return left is right
        return left.normalize() == right.normalize()

    def _existing_success(
        self, owner_user_id: UUID, lifecycle_event_id: UUID
    ) -> Day27ManagementResult | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT payload
                    FROM audit_events
                    WHERE actor_user_id = :owner_user_id
                      AND event_type = 'mt5.day27_management_success'
                      AND payload ->> 'lifecycle_event_id' = :event_id
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"owner_user_id": owner_user_id, "event_id": str(lifecycle_event_id)},
            ).mappings().first()
        if row is None or not isinstance(row["payload"], dict):
            return None
        payload = row["payload"]
        return Day27ManagementResult(
            lifecycle_event_id=lifecycle_event_id,
            signal_id=UUID(str(payload["signal_id"])),
            user_id=owner_user_id,
            actions_requested=int(payload.get("actions_requested", 0)),
            broker_actions_sent=int(payload.get("broker_actions_sent", 0)),
            positions_closed=int(payload.get("positions_closed", 0)),
            positions_modified=int(payload.get("positions_modified", 0)),
            orders_cancelled=int(payload.get("orders_cancelled", 0)),
            external_positions_reconciled=int(payload.get("external_positions_reconciled", 0)),
            already_applied=True,
        )

    def _audit_success(
        self, result: Day27ManagementResult, actions: tuple[dict[str, Any], ...]
    ) -> None:
        payload = {
            "lifecycle_event_id": str(result.lifecycle_event_id),
            "signal_id": str(result.signal_id),
            "actions": actions,
            "actions_requested": result.actions_requested,
            "broker_actions_sent": result.broker_actions_sent,
            "positions_closed": result.positions_closed,
            "positions_modified": result.positions_modified,
            "orders_cancelled": result.orders_cancelled,
            "external_positions_reconciled": result.external_positions_reconciled,
            "account_environment": "demo",
            "manual_position_reopen_attempted": False,
        }
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (actor_user_id, event_type, entity_type, entity_id, payload)
                    VALUES (:actor, 'mt5.day27_management_success', 'signal', :signal, CAST(:payload AS jsonb))
                    """
                ),
                {
                    "actor": result.user_id,
                    "signal": result.signal_id,
                    "payload": json.dumps(payload),
                },
            )
            session.commit()

    def _audit_failure(
        self,
        *,
        owner_user_id: UUID,
        lifecycle_event_id: UUID,
        signal_id: UUID,
        actions: tuple[dict[str, Any], ...],
        counters: dict[str, int],
        error_code: str,
    ) -> None:
        payload = {
            "lifecycle_event_id": str(lifecycle_event_id),
            "signal_id": str(signal_id),
            "actions": actions,
            **counters,
            "error_code": error_code,
            "automatic_reopen_attempted": False,
        }
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (actor_user_id, event_type, entity_type, entity_id, payload)
                    VALUES (:actor, 'mt5.day27_management_failure', 'signal', :signal, CAST(:payload AS jsonb))
                    """
                ),
                {
                    "actor": owner_user_id,
                    "signal": signal_id,
                    "payload": json.dumps(payload),
                },
            )
            session.commit()


__all__ = [
    "Day27ManagementError",
    "Day27ManagementResult",
    "Day27Mt5ManagementService",
]
