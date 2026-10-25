"""Isolated, fail-closed Vantage PAPER -> PU Prime LIVE MT5 master position copier.

Read source positions through the existing legacy bridge. The PU source account
gets only forward, broker-confirmed Super Signals MARKET positions at 0.01 lots.
The canonical Vantage path, Telegram publishing and provider decisions are never
mutated. Existing LocalBridgeCommand idempotency is the execution journal.
A source position with a timed-out PU trade is never sent a second opening order.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.local_bridge_gateways import LocalBridgeReadGateway, LocalBridgeTradeGateway
from app.local_bridge_queue import LocalBridgeQueue
from app.pu_mirror_safety import MirrorRejected, plan_pu_mirror_leg

logger = logging.getLogger(__name__)
_PREFIX = "SS_PU_"


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return result if result.tzinfo is not None else result.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _quote_recent(quote: dict[str, Any], now: datetime) -> bool:
    moment = _as_datetime(quote.get("time"))
    return moment is not None and timedelta(seconds=0) <= now - moment <= timedelta(seconds=45)


def _position_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


def _mirror_client_id(ticket: str) -> str | None:
    if not ticket.isdigit() or len(ticket) > 18:
        return None
    return _PREFIX + ticket


class PuMirrorRuntime:
    """Never enabled by default; observe mode cannot place or amend broker trades."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory
        self.mode = os.getenv("SUPER_SIGNALS_PU_MIRROR_MODE", "off").strip().lower()
        if self.mode not in {"off", "observe", "execute"}:
            raise ValueError("pu_mirror_mode_invalid")
        self.source_account_id = os.getenv("SUPER_SIGNALS_PU_MIRROR_SOURCE_ACCOUNT_ID", "").strip()
        self.target_account_id = os.getenv("SUPER_SIGNALS_PU_BRIDGE_ACCOUNT_ID", "").strip()
        self.owner_user_id = os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip()
        self._since = _as_datetime(os.getenv("SUPER_SIGNALS_PU_MIRROR_ACTIVATE_AFTER", "").strip())
        self._poll_seconds = max(4, int(os.getenv("SUPER_SIGNALS_PU_MIRROR_POLL_SECONDS", "5")))
        self._task: asyncio.Task[None] | None = None
        self._source: LocalBridgeQueue | None = None
        self._target: LocalBridgeQueue | None = None
        self._failure_codes: dict[str, str] = {}
        if self.mode != "off":
            if not self.source_account_id or not self.target_account_id or not self.owner_user_id or self._since is None:
                raise ValueError("pu_mirror_requires_explicit_accounts_owner_and_start_time")
            UUID(self.owner_user_id)
            if self.mode == "execute" and (
                os.getenv("SUPER_SIGNALS_PU_MIRROR_LIVE_CONFIRMED", "").strip()
                != "confirmed-target-36394935"
            ):
                raise ValueError("pu_mirror_live_confirmation_missing")
            self._source = LocalBridgeQueue(
                session_factory, profile="super-signals",
                account_id=self.source_account_id, timeout_seconds=15,
            )
            self._target = LocalBridgeQueue(
                session_factory, profile="pu-prime",
                account_id=self.target_account_id, timeout_seconds=15,
            )

    async def start(self) -> None:
        if self.mode == "off":
            return
        logger.warning(
            "PU MIRROR runtime started mode=%s target=36394935 start=%s "
            "fixed_lot=0.01 no_pending_replay=true",
            self.mode, self._since.isoformat() if self._since else None,
        )
        self._task = asyncio.create_task(self._run(), name="pu-prime-master-mirror")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _find_owned_source(self, broker: dict[str, Any]) -> dict[str, Any] | None:
        ticket, client = _position_id(broker), str(broker.get("clientId") or "")
        if not ticket or not client.startswith("SS_"):
            return None
        with self._factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT p.signal_id::text AS signal_id, p.status, p.created_at,
                           p.tp_index, s.side, s.order_type,
                           s.parser_status, src.status AS source_status,
                           (SELECT COUNT(*) FROM positions p2
                            WHERE p2.signal_id=p.signal_id AND p2.user_id=p.user_id
                              AND p2.broker_position_id IS NOT NULL) AS signal_legs
                    FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    JOIN sources src ON src.id=s.source_id
                    WHERE p.user_id=CAST(:owner AS uuid)
                      AND p.broker_position_id=:ticket
                      AND p.broker_client_id=:client
                      AND s.symbol='XAUUSD' AND p.created_at>=:since
                    LIMIT 1
                    """
                ),
                {
                    "owner": self.owner_user_id,
                    "ticket": ticket, "client": client,
                    "since": self._since,
                },
            ).mappings().first()
        return dict(row) if row else None

    def _trade_command_exists(self, ticket: str) -> bool:
        assert self._target is not None
        client_id = _mirror_client_id(ticket)
        if client_id is None:
            return True
        with self._factory() as session:
            exists = session.execute(
                text(
                    """
                    SELECT 1 FROM local_bridge_commands
                    WHERE idempotency_key=:key LIMIT 1
                    """
                ),
                {"key": f"mt5-order:{self.target_account_id}:{client_id}"},
            ).first()
            return exists is not None

    def _source_row_closed(self, source_ticket: str) -> bool:
        with self._factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT p.status FROM positions p
                    WHERE p.user_id=CAST(:owner AS uuid)
                      AND p.broker_position_id=:ticket
                      AND p.created_at>=:since
                    LIMIT 1
                    """
                ),
                {
                    "owner": self.owner_user_id, "ticket": source_ticket,
                    "since": self._since,
                },
            ).first()
        return bool(row and row[0] == "closed")

    async def _queue_read(
        self, queue: LocalBridgeQueue, account_id: str, operation: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        return await queue.execute(
            account_id=account_id, operation=operation, payload=payload or {},
        )

    @staticmethod
    def _open_risk_usd(
        positions: list[dict[str, Any]], quote: dict[str, Any], specification: dict[str, Any],
    ) -> Decimal:
        tick = Decimal(str(specification["tickSize"]))
        loss_tick_value = Decimal(str(quote["lossTickValue"]))
        total = Decimal("0")
        if tick <= 0 or loss_tick_value <= 0:
            raise MirrorRejected("pu_open_risk_spec_invalid")
        for position in positions:
            if position.get("symbol") != "XAUUSD.s":
                continue
            if not str(position.get("clientId") or "").startswith(_PREFIX):
                raise MirrorRejected("unmapped_pu_gold_exposure")
            stop, entry, volume = (
                position.get("stopLoss"),
                position.get("openPrice"),
                position.get("volume"),
            )
            if not stop or not entry or not volume:
                raise MirrorRejected("pu_gold_exposure_unprotected")
            total += abs(Decimal(str(entry)) - Decimal(str(stop))) / tick * loss_tick_value * Decimal(str(volume))
        return total

    async def _consider_entry(
        self, source_pos: dict[str, Any], signal: dict[str, Any],
        pu_positions: list[dict[str, Any]], account: dict[str, Any],
        quote: dict[str, Any], specification: dict[str, Any],
    ) -> None:
        assert self._target is not None
        ticket = _position_id(source_pos)
        client_id = _mirror_client_id(ticket)
        if client_id is None or self._trade_command_exists(ticket):
            return
        if signal.get("status") != "open" or signal.get("parser_status") != "accepted":
            return
        if signal.get("source_status") not in {"live", "testing"}:
            return
        if str(signal.get("order_type") or "").lower() != "market":
            return
        age = datetime.now(UTC) - signal["created_at"]
        if age < timedelta(0) or age > timedelta(seconds=120):
            return
        side = "BUY" if source_pos.get("type") == "POSITION_TYPE_BUY" else "SELL"
        if side != signal.get("side") or source_pos.get("symbol") != "XAUUSD":
            return
        if not _quote_recent(quote, datetime.now(UTC)):
            return
        ask_or_bid = quote["ask"] if side == "BUY" else quote["bid"]
        try:
            margin_result = await self._queue_read(
                self._target, self.target_account_id,
                "calculate_margin",
                {
                    "symbol": "XAUUSD", "side": side, "volume": 0.01,
                    "open_price": float(ask_or_bid),
                },
            )
            committed = self._open_risk_usd(pu_positions, quote, specification)
            plan = plan_pu_mirror_leg(
                source=source_pos, account=account, quote=quote,
                specification=specification,
                calculated_margin=margin_result["margin"],
                source_signal_legs=int(signal["signal_legs"]),
                existing_risk_usd=committed,
            )
        except (MirrorRejected, KeyError, ValueError, ArithmeticError) as exc:
            code = exc.code if isinstance(exc, MirrorRejected) else "invalid_broker_or_position_data"
            if self._failure_codes.get(ticket) != code:
                logger.warning("PU MIRROR source=%s entry BLOCKED code=%s", ticket, code)
                self._failure_codes[ticket] = code
            return
        if self.mode != "execute":
            if self._failure_codes.get(ticket) != "observe_candidate":
                logger.info("PU MIRROR OBSERVE source=%s tp=%s risk=%s amount=0.01",
                            ticket, signal.get("tp_index"), plan.stop_risk_usd)
                self._failure_codes[ticket] = "observe_candidate"
            return
        logger.warning(
            "PU MIRROR placing source=%s tp=%s volume=%s risk=%s",
            ticket, signal.get("tp_index"), plan.volume, plan.stop_risk_usd,
        )
        gateway = LocalBridgeTradeGateway(
            self._target, read_gateway=LocalBridgeReadGateway(self._target),
        )
        # Idempotent queue key is derived from broker account + immutable clientId.
        # Ambiguous executions remain journalled, never re-issued as a new order.
        result = await gateway.place_market_order(
            token="", account_id=self.target_account_id, region="local",
            side=plan.side, symbol=plan.symbol, volume=float(plan.volume),
            stop_loss=float(plan.stop_loss),
            take_profit=float(plan.take_profit) if plan.take_profit is not None else None,
            client_id=client_id,
        )
        logger.warning(
            "PU MIRROR confirmed order source=%s order=%s position=%s",
            ticket, result.order_id, result.position_id,
        )

    async def _manage_existing(
        self, source_by_ticket: dict[str, dict[str, Any]],
        pu_positions: list[dict[str, Any]],
    ) -> None:
        assert self._target is not None
        if self.mode != "execute":
            return
        for target in pu_positions:
            client = str(target.get("clientId") or "")
            if not client.startswith(_PREFIX):
                continue
            source_ticket = client[len(_PREFIX):]
            if _mirror_client_id(source_ticket) != client:
                continue
            ticket = _position_id(target)
            if not ticket or target.get("symbol") != "XAUUSD.s":
                continue
            source = source_by_ticket.get(source_ticket)
            if source is None:
                if not await asyncio.to_thread(self._source_row_closed, source_ticket):
                    logger.error(
                        "PU MIRROR orphan requires source settlement proof source=%s target=%s",
                        source_ticket, ticket,
                    )
                    continue
                await self._target.execute(
                    account_id=self.target_account_id,
                    operation="trade",
                    payload={"actionType": "POSITION_CLOSE_ID", "positionId": ticket},
                    idempotency_key=f"pu:close:{source_ticket}:{ticket}",
                )
                logger.warning("PU MIRROR closed settled source=%s target=%s", source_ticket, ticket)
                continue
            proof = await asyncio.to_thread(self._find_owned_source, source)
            if proof is None:
                continue
            proposed = source.get("stopLoss")
            old = target.get("stopLoss")
            if proposed is None or old is None:
                continue
            try:
                next_sl, old_sl = Decimal(str(proposed)), Decimal(str(old))
                is_buy = target.get("type") == "POSITION_TYPE_BUY"
                protective = next_sl > old_sl if is_buy else next_sl < old_sl
                if not protective or next_sl <= 0:
                    continue
                # MT5 treats MODIFY as replacement. Preserve target TP exactly.
                target_tp = float(target.get("takeProfit") or 0)
                await self._target.execute(
                    account_id=self.target_account_id,
                    operation="trade",
                    payload={
                        "actionType": "POSITION_MODIFY",
                        "positionId": ticket,
                        "stopLoss": float(next_sl),
                        "takeProfit": target_tp,
                    },
                    idempotency_key=f"pu:sl:{source_ticket}:{ticket}:{next_sl}",
                )
                logger.warning(
                    "PU MIRROR tightened source=%s target=%s stop=%s",
                    source_ticket, ticket, next_sl,
                )
            except (ValueError, ArithmeticError):
                logger.error("PU MIRROR invalid protective stop source=%s", source_ticket)

    async def _cycle(self) -> None:
        assert self._source is not None and self._target is not None
        identity = await self._queue_read(self._target, self.target_account_id, "read_terminal_identity")
        if (not isinstance(identity, dict) or identity.get("login") != 36394935
                or identity.get("server") != "PUPrime-Live 6"):
            raise MirrorRejected("pu_master_identity_mismatch")
        account = await self._queue_read(self._target, self.target_account_id, "read_account_information")
        if not isinstance(account, dict) or account.get("tradeAllowed") is not True:
            raise MirrorRejected("pu_master_trading_disabled")
        source_positions = await self._queue_read(self._source, self.source_account_id, "read_positions")
        target_positions = await self._queue_read(self._target, self.target_account_id, "read_positions")
        if not isinstance(source_positions, list) or not isinstance(target_positions, list):
            raise MirrorRejected("invalid_broker_position_snapshot")
        source_by_ticket = {_position_id(p): p for p in source_positions if isinstance(p, dict)}
        if len(source_by_ticket) != len(source_positions):
            raise MirrorRejected("duplicate_or_invalid_source_positions")
        # Always settle existing copied exposure even when no new entries qualify.
        await self._manage_existing(source_by_ticket, target_positions)
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for position in source_positions:
            if not isinstance(position, dict) or position.get("symbol") != "XAUUSD":
                continue
            ticket = _position_id(position)
            if _mirror_client_id(ticket) is None or self._trade_command_exists(ticket):
                continue
            signal = await asyncio.to_thread(self._find_owned_source, position)
            if signal is not None:
                candidates.append((position, signal))
        if not candidates:
            return
        spec = await self._queue_read(
            self._target, self.target_account_id,
            "read_symbol_specification", {"symbol": "XAUUSD"},
        )
        quote = await self._queue_read(
            self._target, self.target_account_id,
            "read_symbol_price", {"symbol": "XAUUSD"},
        )
        if not isinstance(spec, dict) or not isinstance(quote, dict):
            raise MirrorRejected("pu_quote_spec_invalid")
        for source_pos, signal in candidates[:10]:
            await self._consider_entry(
                source_pos, signal, target_positions, account, quote, spec,
            )

    async def _run(self) -> None:
        while True:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "PU MIRROR cycle held safely code=%s",
                    exc.code if isinstance(exc, MirrorRejected) else type(exc).__name__,
                )
            await asyncio.sleep(self._poll_seconds)
