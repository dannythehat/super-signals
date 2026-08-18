"""Broker-authoritative pending-order fill reconciliation for paper testing.

Pending orders live at Vantage/MT5, not in a local price watcher. This manager only
observes the Owner's DEMO account. When a mapped clientId appears as a real broker
position, the existing local tranche transitions ``pending -> open`` and records the
broker fill price/position ID. It never creates, retries, chases or reopens a trade.
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
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PaperPendingReconcileResult:
    pending_seen: int
    fills_mapped: int
    still_pending: int
    unresolved: int


class PaperPendingReconciler:
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
            raise ValueError("paper_pending_poll_seconds_invalid")
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
            name="super-signals-paper-pending-reconciler",
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
                if result.fills_mapped:
                    logger.info(
                        "Paper pending reconciliation mapped fills=%d still_pending=%d",
                        result.fills_mapped,
                        result.still_pending,
                    )
                if result.unresolved:
                    logger.warning(
                        "Paper pending reconciliation unresolved=%d; no broker mutation attempted",
                        result.unresolved,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Paper pending reconciliation failed safely")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def reconcile_once(self) -> PaperPendingReconcileResult:
        rows = self._pending_rows()
        if not rows:
            return PaperPendingReconcileResult(0, 0, 0, 0)

        account = self._demo_account()
        if account is None:
            logger.error("Paper pending reconciliation blocked: connected DEMO account unavailable")
            return PaperPendingReconcileResult(len(rows), 0, 0, len(rows))
        account_id, ciphertext = account
        try:
            token = self._cipher.decrypt(ciphertext).strip()
        except BrokerCredentialDecryptionError:
            logger.error("Paper pending reconciliation blocked: broker credential decryption failed")
            return PaperPendingReconcileResult(len(rows), 0, 0, len(rows))
        if len(token) < 20:
            return PaperPendingReconcileResult(len(rows), 0, 0, len(rows))

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
            logger.warning("Paper pending reconciliation read blocked code=%s", exc.code)
            return PaperPendingReconcileResult(len(rows), 0, 0, len(rows))

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
        for row in rows:
            broker = by_client.get(row["broker_client_id"])
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

            if row["broker_order_id"] in active_order_ids:
                still_pending += 1
                continue

            # An order may disappear from /orders a fraction before its resulting
            # position becomes visible. Leave it unresolved and re-read next poll;
            # never infer a cancellation or a fill from absence alone.
            unresolved += 1
            self._audit_unresolved(row, "pending_order_not_visible_at_broker")

        return PaperPendingReconcileResult(
            pending_seen=len(rows),
            fills_mapped=mapped,
            still_pending=still_pending,
            unresolved=unresolved,
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

    def _demo_account(self) -> tuple[str, bytes] | None:
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
        if value is None:
            return None
        if isinstance(value, bool):
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
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        actor_user_id, event_type, entity_type, entity_id, payload
                    ) VALUES (
                        :actor, 'mt5.paper_pending_fill_mapped', 'position', :position,
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "actor": self._owner_user_id,
                    "position": local_id,
                    "payload": (
                        '{"broker_authoritative":true,"paper_demo_only":true,'
                        '"trade_action_created":false}'
                    ),
                },
            )
            session.commit()

    def _audit_unresolved(self, row: dict[str, object], code: str) -> None:
        # De-duplicate noisy polls to one audit row per position/code/hour while the
        # in-memory log still exposes every current reconciliation issue.
        with self._session_factory() as session:
            exists = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM audit_events
                            WHERE entity_type='position'
                              AND entity_id=:position
                              AND event_type='mt5.paper_pending_reconcile_unresolved'
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
            payload = (
                '{"code":"' + code.replace('"', "") + '",'
                '"paper_demo_only":true,"trade_action_created":false}'
            )
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        actor_user_id, event_type, entity_type, entity_id, payload
                    ) VALUES (
                        :actor, 'mt5.paper_pending_reconcile_unresolved', 'position',
                        :position, CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "actor": self._owner_user_id,
                    "position": row["id"],
                    "payload": payload,
                },
            )
            session.commit()


__all__ = ["PaperPendingReconcileResult", "PaperPendingReconciler"]
