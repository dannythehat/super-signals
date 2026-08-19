"""Broker-authoritative pending-order fill and terminal-state reconciliation.

Pending orders live at Vantage/MT5, never in a local price watcher. This reconciler is
shared by the Owner paper account and eligible future LIVE member accounts. Account
eligibility differs; broker-truth interpretation does not.

A mapped clientId appearing as a real broker position transitions the existing local
tranche ``pending -> open``. If an order is no longer active, the exact MT order ticket
is checked in immutable broker history before local state changes. Absence alone never
means cancelled or filled.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

logger = logging.getLogger(__name__)
_TERMINAL_NO_FILL_STATES = {
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
}
_FILLED_STATES = {"ORDER_STATE_FILLED", "ORDER_STATE_PARTIAL"}


@dataclass(frozen=True, slots=True)
class PendingReconcileResult:
    pending_seen: int
    fills_mapped: int
    still_pending: int
    unresolved: int
    terminalized: int = 0


class AccountPendingReconciler:
    """Reconcile one eligible account without creating or chasing any order."""

    account_environment = "demo"

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        gateway: MetaApiReadGateway,
        owner_user_id: UUID,
        poll_seconds: int = 3,
    ) -> None:
        if poll_seconds < 1:
            raise ValueError("pending_poll_seconds_invalid")
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway
        self._owner_user_id = owner_user_id
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(),
            name=f"super-signals-{self.account_environment}-pending-reconciler",
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = await self.reconcile_once()
                if result.fills_mapped or result.terminalized:
                    logger.info(
                        "Pending reconciliation environment=%s fills=%d terminalized=%d still_pending=%d",
                        self.account_environment,
                        result.fills_mapped,
                        result.terminalized,
                        result.still_pending,
                    )
                if result.unresolved:
                    logger.warning(
                        "Pending reconciliation environment=%s unresolved=%d; no broker mutation attempted",
                        self.account_environment,
                        result.unresolved,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pending reconciliation failed safely")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def reconcile_once(self) -> PendingReconcileResult:
        rows = self._pending_rows()
        if not rows:
            return PendingReconcileResult(0, 0, 0, 0, 0)

        account = self._broker_account()
        if account is None:
            logger.error(
                "Pending reconciliation blocked: eligible connected %s account unavailable",
                self.account_environment,
            )
            return PendingReconcileResult(len(rows), 0, 0, len(rows), 0)
        account_id, ciphertext = account
        try:
            token = self._cipher.decrypt(ciphertext).strip()
        except BrokerCredentialDecryptionError:
            logger.error("Pending reconciliation blocked: broker credential decryption failed")
            return PendingReconcileResult(len(rows), 0, 0, len(rows), 0)
        if len(token) < 20:
            return PendingReconcileResult(len(rows), 0, 0, len(rows), 0)

        try:
            region = await self._gateway.resolve_account_region(
                token=token,
                account_id=account_id,
            )
            broker_positions = await self._gateway.read_positions(
                token=token,
                account_id=account_id,
                region=region,
            )
            broker_orders = await self._gateway.read_orders(
                token=token,
                account_id=account_id,
                region=region,
            )
        except MetaApiGatewayError as exc:
            logger.warning("Pending reconciliation read blocked code=%s", exc.code)
            return PendingReconcileResult(len(rows), 0, 0, len(rows), 0)

        by_client = {
            str(item.get("clientId") or "").strip(): item
            for item in broker_positions
            if str(item.get("clientId") or "").strip()
        }
        active_order_ids = {
            str(item.get("id") or "").strip()
            for item in broker_orders
            if str(item.get("id") or "").strip()
        }

        mapped = 0
        still_pending = 0
        unresolved = 0
        terminalized = 0
        for row in rows:
            broker = by_client.get(str(row["broker_client_id"] or ""))
            if broker is not None:
                try:
                    position_id, open_price = self._validate_fill(row, broker)
                except ValueError as exc:
                    unresolved += 1
                    self._audit_unresolved(row, str(exc))
                    continue
                self._persist_fill(row["id"], position_id, open_price)
                mapped += 1
                continue

            order_id = str(row["broker_order_id"] or "").strip()
            if order_id in active_order_ids:
                still_pending += 1
                continue

            try:
                history = await self._gateway.read_history_orders_by_ticket(
                    token=token,
                    account_id=account_id,
                    region=region,
                    order_id=order_id,
                )
            except MetaApiGatewayError as exc:
                unresolved += 1
                self._audit_unresolved(row, f"pending_history_read_failed:{exc.code}")
                continue

            try:
                terminal = self._matching_history_order(row, history)
            except ValueError as exc:
                unresolved += 1
                self._audit_unresolved(row, str(exc))
                continue

            if terminal is None:
                unresolved += 1
                self._audit_unresolved(row, "pending_order_not_visible_at_broker")
                continue

            state = str(terminal.get("state") or "").strip().upper()
            if state in _TERMINAL_NO_FILL_STATES:
                self._persist_terminal_no_fill(row, terminal, state)
                terminalized += 1
                continue

            if state in _FILLED_STATES:
                # History proves that this ticket is no longer pending. If the resulting
                # position is absent from the current snapshot, do not fabricate whether
                # it remains open or is already closed; mark it for account/deal settlement.
                self._persist_filled_not_visible(row, terminal, state)
                terminalized += 1
                unresolved += 1
                continue

            unresolved += 1
            self._audit_unresolved(row, f"pending_history_state_unresolved:{state or 'missing'}")

        return PendingReconcileResult(
            pending_seen=len(rows),
            fills_mapped=mapped,
            still_pending=still_pending,
            unresolved=unresolved,
            terminalized=terminalized,
        )

    def _pending_rows(self) -> list[dict[str, object]]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT p.id, p.signal_id, p.entry_index, p.tp_index,
                           p.broker_client_id, p.broker_order_id, p.volume,
                           p.stop_loss, p.take_profit, s.symbol, s.side
                    FROM positions AS p
                    JOIN signals AS s ON s.id=p.signal_id
                    WHERE p.user_id=:user_id
                      AND p.status='pending'
                      AND p.broker_client_id IS NOT NULL
                      AND p.broker_order_id IS NOT NULL
                    ORDER BY p.created_at
                    """
                ),
                {"user_id": self._owner_user_id},
            ).mappings().all()
        return [dict(row) for row in rows]

    def _broker_account(self) -> tuple[str, bytes] | None:
        """Return the Owner reference DEMO account; LIVE subclass overrides eligibility."""
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id, metaapi_token_ciphertext
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND status='connected'
                      AND account_environment='demo'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": self._owner_user_id},
            ).mappings().first()
        if row is None:
            return None
        return str(row["metaapi_account_id"]), bytes(row["metaapi_token_ciphertext"])

    @classmethod
    def _matching_history_order(
        cls,
        local: dict[str, object],
        history: list[dict[str, object]],
    ) -> dict[str, object] | None:
        expected_order_id = str(local.get("broker_order_id") or "").strip()
        matches = [
            item
            for item in history
            if str(item.get("id") or "").strip() == expected_order_id
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("pending_history_ticket_ambiguous")
        item = matches[0]
        expected_client = str(local.get("broker_client_id") or "").strip()
        broker_client = str(item.get("clientId") or "").strip()
        if broker_client and expected_client and broker_client != expected_client:
            raise ValueError("pending_history_client_id_mismatch")
        if str(item.get("symbol") or "").strip().upper() != str(local["symbol"]).upper():
            raise ValueError("pending_history_symbol_mismatch")
        raw_type = str(item.get("type") or "").strip().upper()
        broker_side = "BUY" if "BUY" in raw_type else "SELL" if "SELL" in raw_type else ""
        if broker_side and broker_side != str(local["side"]).upper():
            raise ValueError("pending_history_side_mismatch")
        return item

    @classmethod
    def _validate_fill(
        cls,
        local: dict[str, object],
        broker: dict[str, object],
    ) -> tuple[str, Decimal]:
        position_id = str(broker.get("id") or "").strip()
        if not position_id:
            raise ValueError("pending_fill_position_id_missing")
        if str(broker.get("symbol") or "").strip().upper() != str(local["symbol"]).upper():
            raise ValueError("pending_fill_symbol_mismatch")
        raw_type = str(broker.get("type") or "")
        broker_side = (
            "BUY"
            if raw_type == "POSITION_TYPE_BUY"
            else "SELL"
            if raw_type == "POSITION_TYPE_SELL"
            else raw_type.upper()
        )
        if broker_side != str(local["side"]).upper():
            raise ValueError("pending_fill_side_mismatch")

        expected_volume = cls._decimal(local.get("volume"))
        broker_volume = cls._decimal(broker.get("volume"))
        expected_sl = cls._decimal(local.get("stop_loss"))
        broker_sl = cls._decimal(broker.get("stopLoss"))
        expected_tp = cls._decimal(local.get("take_profit"), optional=True)
        broker_tp = cls._decimal(broker.get("takeProfit"), optional=True)
        open_price = cls._decimal(broker.get("openPrice"))
        if (
            expected_volume is None
            or broker_volume != expected_volume
            or expected_sl is None
            or broker_sl != expected_sl
            or open_price is None
        ):
            raise ValueError("pending_fill_mapping_mismatch")
        if expected_tp is None:
            if broker_tp not in {None, Decimal("0")}:
                raise ValueError("pending_fill_tp_mismatch")
        elif broker_tp != expected_tp:
            raise ValueError("pending_fill_tp_mismatch")
        return position_id, open_price

    @staticmethod
    def _decimal(value: object, *, optional: bool = False) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not parsed.is_finite():
            return None
        if optional and parsed == 0:
            return Decimal("0")
        return parsed if parsed > 0 else None

    def _persist_fill(self, local_id: object, position_id: str, open_price: Decimal) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_position_id=:position_id,
                        entry_price=:open_price,
                        status='open',
                        opened_at=COALESCE(opened_at,:now),
                        close_reason=NULL,
                        updated_at=:now
                    WHERE id=:id AND status='pending'
                    """
                ),
                {
                    "id": local_id,
                    "position_id": position_id,
                    "open_price": open_price,
                    "now": now,
                },
            )
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.pending_fill_mapped",
                    entity_type="position",
                    entity_id=local_id,
                    payload={
                        "broker_authoritative": True,
                        "account_environment": self.account_environment,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _persist_terminal_no_fill(
        self,
        row: dict[str, object],
        history: dict[str, object],
        state: str,
    ) -> None:
        now = datetime.now(UTC)
        reason = f"broker_order_{state.lower().removeprefix('order_state_')}"[:80]
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='skipped', close_reason=:reason,
                        closed_at=COALESCE(closed_at,:now), updated_at=:now
                    WHERE id=:id AND status='pending'
                    """
                ),
                {"id": row["id"], "reason": reason, "now": now},
            )
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.pending_broker_terminal_no_fill",
                    entity_type="position",
                    entity_id=row["id"],
                    payload={
                        "broker_authoritative": True,
                        "account_environment": self.account_environment,
                        "broker_order_id": str(row["broker_order_id"]),
                        "broker_state": state,
                        "done_time": history.get("doneTime"),
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _persist_filled_not_visible(
        self,
        row: dict[str, object],
        history: dict[str, object],
        state: str,
    ) -> None:
        now = datetime.now(UTC)
        position_id = str(history.get("positionId") or "").strip() or None
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_position_id=COALESCE(:position_id,broker_position_id),
                        status='error',
                        close_reason='broker_filled_position_not_visible',
                        updated_at=:now
                    WHERE id=:id AND status='pending'
                    """
                ),
                {"id": row["id"], "position_id": position_id, "now": now},
            )
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.pending_broker_fill_requires_settlement",
                    entity_type="position",
                    entity_id=row["id"],
                    payload={
                        "broker_authoritative": True,
                        "account_environment": self.account_environment,
                        "broker_order_id": str(row["broker_order_id"]),
                        "broker_position_id": position_id,
                        "broker_state": state,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _audit_unresolved(self, row: dict[str, object], code: str) -> None:
        with self._session_factory() as session:
            exists = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM audit_events
                            WHERE entity_type='position'
                              AND entity_id=:position
                              AND event_type='mt5.pending_reconcile_unresolved'
                              AND payload->>'code'=:code
                              AND created_at > now() - interval '1 hour'
                        )
                        """
                    ),
                    {"position": row["id"], "code": code},
                ).scalar_one()
            )
            if exists:
                return
            session.add(
                AuditEvent(
                    actor_user_id=self._owner_user_id,
                    event_type="mt5.pending_reconcile_unresolved",
                    entity_type="position",
                    entity_id=row["id"],
                    payload={
                        "code": code,
                        "account_environment": self.account_environment,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()


__all__ = ["AccountPendingReconciler", "PendingReconcileResult"]
