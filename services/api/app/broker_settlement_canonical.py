"""Canonical broker-led settlement and account-truth polling.

Broker history is authoritative whether trades are currently unsettled or the account is
flat. Active trades keep the normal settlement cadence. While flat, the manager performs
a read-only account/deal sync at most once per minute so balance operations and delayed
broker history cannot leave the app stale.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher
from app.models import AuditEvent

from app.broker_settlement_day34 import (
    Day34BrokerSettlementManager,
    Day34SettlementPollResult,
)
from app.performance_ledger_day33 import Day33LedgerError

_FLAT_ACCOUNT_SYNC_SECONDS = 60.0
_TARGET_PRICE_TOLERANCE = Decimal("0.75")

logger = logging.getLogger(__name__)


class CanonicalBrokerSettlementManager(Day34BrokerSettlementManager):
    """One settlement manager for active-position and flat-account broker truth."""

    def __init__(
        self,
        *,
        cipher: MetaApiTokenCipher | None = None,
        read_gateway: MetaApiReadGateway | None = None,
        trade_gateway: MetaApiTradeGateway | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._cipher = cipher
        self._read = read_gateway
        self._trade = trade_gateway

    async def poll_once(self) -> Day34SettlementPollResult:
        if self._has_unsettled_mapped_positions():
            result = await super().poll_once()
            await self._apply_profit_protection_ladder()
            return result

        now = time.monotonic()
        last = float(getattr(self, "_account_truth_last_sync", 0.0) or 0.0)
        if last and now - last < _FLAT_ACCOUNT_SYNC_SECONDS:
            return Day34SettlementPollResult(
                synced=False,
                positions_reconciled=0,
                position_events_created=0,
                signal_results_created=0,
                reason="flat_account_truth_sync_not_due",
            )

        try:
            await self._performance.sync_user(self._reference_user_id)
        except Day33LedgerError as exc:
            self._audit_poll_failure(exc.code, retryable=exc.retryable)
            return Day34SettlementPollResult(
                synced=False,
                positions_reconciled=0,
                position_events_created=0,
                signal_results_created=0,
                reason=exc.code,
            )

        self._account_truth_last_sync = now
        return Day34SettlementPollResult(
            synced=True,
            positions_reconciled=0,
            position_events_created=0,
            signal_results_created=0,
            reason="flat_account_truth_sync_complete",
        )

    async def _apply_profit_protection_ladder(self) -> None:
        """Protect broker-confirmed winners without waiting for provider management.

        TP2 hit:
        * cancel every unused entry order for the setup;
        * move every open TP3+ leg to its own entry (true per-leg breakeven).

        TP3 hit:
        * move every remaining TP4+/runner stop to the signal's TP2 price.

        Existing protection is never loosened. The next settlement poll retries any
        broker mutation which did not become locally confirmed.
        """
        if self._cipher is None or self._read is None or self._trade is None:
            return
        account = self._broker_account()
        if account is None:
            return
        account_id, ciphertext = account
        try:
            token = self._cipher.decrypt(ciphertext)
            region = await self._read.resolve_account_region(
                token=token,
                account_id=account_id,
            )
            broker_positions = {
                str(item.get("id") or ""): item
                for item in await self._read.read_positions(
                    token=token,
                    account_id=account_id,
                    region=region,
                )
            }
            broker_orders = {
                str(item.get("id") or ""): item
                for item in await self._read.read_orders(
                    token=token,
                    account_id=account_id,
                    region=region,
                )
            }
        except (BrokerCredentialDecryptionError, MetaApiGatewayError) as exc:
            self._audit_protection_failure(None, getattr(exc, "code", type(exc).__name__))
            return

        for plan in self._protection_plans():
            signal_id = UUID(str(plan["signal_id"]))
            try:
                if bool(plan["tp2_hit"]):
                    await self._cancel_pending_for_signal(
                        signal_id=signal_id,
                        token=token,
                        account_id=account_id,
                        region=region,
                        broker_orders=broker_orders,
                    )
                    await self._protect_open_for_signal(
                        signal_id=signal_id,
                        minimum_tp_index=3,
                        target="entry",
                        token=token,
                        account_id=account_id,
                        region=region,
                        broker_positions=broker_positions,
                    )
                if bool(plan["tp3_hit"]) and plan["tp2_price"] is not None:
                    await self._protect_open_for_signal(
                        signal_id=signal_id,
                        minimum_tp_index=4,
                        target=Decimal(str(plan["tp2_price"])),
                        token=token,
                        account_id=account_id,
                        region=region,
                        broker_positions=broker_positions,
                    )
            except MetaApiGatewayError as exc:
                self._audit_protection_failure(signal_id, exc.code)

    def _broker_account(self) -> tuple[str, bytes] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id,metaapi_token_ciphertext
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND account_environment='demo'
                      AND status='connected'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": self._reference_user_id},
            ).mappings().first()
        if row is None:
            return None
        return str(row["metaapi_account_id"]), bytes(row["metaapi_token_ciphertext"])

    def _protection_plans(self) -> tuple[Any, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        s.id AS signal_id,
                        MAX(p.take_profit) FILTER (WHERE p.tp_index=2) AS tp2_price,
                        BOOL_OR(
                            p.tp_index=2
                            AND p.status='closed'
                            AND p.pnl_amount>0
                            AND p.take_profit IS NOT NULL
                            AND p.exit_price IS NOT NULL
                            AND ABS(p.exit_price-p.take_profit)<=:tolerance
                        ) AS tp2_hit,
                        BOOL_OR(
                            p.tp_index=3
                            AND p.status='closed'
                            AND p.pnl_amount>0
                            AND p.take_profit IS NOT NULL
                            AND p.exit_price IS NOT NULL
                            AND ABS(p.exit_price-p.take_profit)<=:tolerance
                        ) AS tp3_hit
                    FROM signals AS s
                    JOIN positions AS p ON p.signal_id=s.id
                    WHERE p.user_id=:user_id
                    GROUP BY s.id
                    HAVING (
                        BOOL_OR(p.tp_index=2 AND p.status='closed' AND p.pnl_amount>0)
                        AND BOOL_OR(p.status IN ('open','pending') AND p.tp_index>=3)
                    ) OR (
                        BOOL_OR(p.tp_index=3 AND p.status='closed' AND p.pnl_amount>0)
                        AND BOOL_OR(p.status='open' AND p.tp_index>=4)
                    )
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "tolerance": _TARGET_PRICE_TOLERANCE,
                },
            ).mappings().all()
        return tuple(rows)

    async def _cancel_pending_for_signal(
        self,
        *,
        signal_id: UUID,
        token: str,
        account_id: str,
        region: str,
        broker_orders: dict[str, dict[str, object]],
    ) -> None:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id,broker_order_id
                    FROM positions
                    WHERE signal_id=:signal_id AND user_id=:user_id
                      AND status='pending' AND broker_order_id IS NOT NULL
                    """
                ),
                {"signal_id": signal_id, "user_id": self._reference_user_id},
            ).mappings().all()
        for row in rows:
            order_id = str(row["broker_order_id"])
            # A locally pending row whose order has disappeared may already have
            # filled. Never falsely mark that row cancelled; the pending reconciler
            # will map the broker position and the next poll will protect it.
            if order_id not in broker_orders:
                continue
            await self._trade.cancel_order(
                token=token,
                account_id=account_id,
                region=region,
                order_id=order_id,
            )
            self._confirm_pending_cancel(UUID(str(row["id"])), signal_id)

    async def _protect_open_for_signal(
        self,
        *,
        signal_id: UUID,
        minimum_tp_index: int,
        target: str | Decimal,
        token: str,
        account_id: str,
        region: str,
        broker_positions: dict[str, dict[str, object]],
    ) -> None:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id,tp_index,entry_price,stop_loss,broker_position_id
                    FROM positions
                    WHERE signal_id=:signal_id AND user_id=:user_id
                      AND status='open' AND tp_index>=:minimum_tp_index
                      AND broker_position_id IS NOT NULL
                    """
                ),
                {
                    "signal_id": signal_id,
                    "user_id": self._reference_user_id,
                    "minimum_tp_index": minimum_tp_index,
                },
            ).mappings().all()
        for row in rows:
            broker_id = str(row["broker_position_id"])
            broker = broker_positions.get(broker_id)
            if broker is None:
                continue
            desired = (
                Decimal(str(row["entry_price"]))
                if target == "entry"
                else Decimal(str(target))
            )
            side = str(broker.get("type") or "").upper()
            current_raw = broker.get("stopLoss")
            current = Decimal(str(current_raw)) if current_raw not in (None, 0, "0") else None
            tightens = (
                current is None
                or ("BUY" in side and desired > current)
                or ("SELL" in side and desired < current)
            )
            if not tightens:
                continue
            await self._trade.modify_position(
                token=token,
                account_id=account_id,
                region=region,
                position_id=broker_id,
                stop_loss=float(desired),
            )
            self._confirm_stop_protection(
                UUID(str(row["id"])),
                signal_id,
                desired,
                minimum_tp_index,
            )

    def _confirm_pending_cancel(self, position_id: UUID, signal_id: UUID) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions SET status='cancelled',updated_at=now()
                    WHERE id=:position_id AND status='pending'
                    """
                ),
                {"position_id": position_id},
            )
            session.add(
                AuditEvent(
                    actor_user_id=self._reference_user_id,
                    event_type="mt5.automatic_profit_protection_pending_cancelled",
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={"position_id": str(position_id), "trigger": "tp2_hit"},
                )
            )
            session.commit()

    def _confirm_stop_protection(
        self,
        position_id: UUID,
        signal_id: UUID,
        stop_loss: Decimal,
        trigger_tp: int,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    "UPDATE positions SET stop_loss=:stop_loss,updated_at=now() WHERE id=:position_id"
                ),
                {"position_id": position_id, "stop_loss": stop_loss},
            )
            session.add(
                AuditEvent(
                    actor_user_id=self._reference_user_id,
                    event_type="mt5.automatic_profit_protection_applied",
                    entity_type="signal",
                    entity_id=signal_id,
                    payload={
                        "position_id": str(position_id),
                        "stop_loss": str(stop_loss),
                        "trigger": "tp2_hit" if trigger_tp == 3 else "tp3_hit",
                        "protection_never_loosened": True,
                    },
                )
            )
            session.commit()

    def _audit_protection_failure(self, signal_id: UUID | None, code: str) -> None:
        logger.error("Automatic profit protection failed signal=%s code=%s", signal_id, code)
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=self._reference_user_id,
                    event_type="mt5.automatic_profit_protection_critical_failure",
                    entity_type="signal" if signal_id else "mt5_account",
                    entity_id=signal_id,
                    payload={"error_code": code, "retryable_on_next_poll": True},
                )
            )
            session.commit()


__all__ = ["CanonicalBrokerSettlementManager"]
