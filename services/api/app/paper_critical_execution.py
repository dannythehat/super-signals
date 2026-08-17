"""Critical pending/layer execution for the Vantage DEMO paper-test boundary.

This service extends the proven Day 28 owner executor only for structures that the old
engine could not represent: explicit broker pending orders and explicit numbered entry
layers. It is deliberately DEMO-only. Ordinary exact/zone market trades continue down
the existing Day 28 path unchanged.

Safety invariants:
* no delayed/chased market entry;
* pending orders are held broker-side at the provider's exact price;
* declared entry layers share the existing per-TP risk budget instead of multiplying it;
* every TP/runner remains a separately mapped tranche;
* any submission failure triggers best-effort exact-ID compensation (cancel pending,
  close opened market legs) and never retries a trade order.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.critical_entry_policy import CriticalEntry, parse_critical_entries
from app.day28_zone_guard import Day28GuardedExecutionService
from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    _AccountInput,
    _SignalInput,
)
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError
from app.paper_pending_gateway import PaperPendingOrderGateway, PaperPendingOrderRequest
from app.risk_sizing_day24 import Day24RiskSizingResult

logger = logging.getLogger(__name__)
_VERIFY_ATTEMPTS = 3
_VERIFY_DELAY_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class CriticalPaperTranche:
    local_position_id: UUID
    entry_index: int
    tp_index: int
    order_type: str
    take_profit: Decimal | None
    volume: Decimal
    client_id: str
    broker_order_id: str
    broker_position_id: str | None
    entry_price: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class CriticalPaperExecutionResult:
    signal_id: UUID
    user_id: UUID
    symbol: str
    side: str
    signal_entry_price: Decimal
    stop_loss: Decimal
    base_risk_percent: Decimal
    effective_risk_percent: Decimal
    double_lot_applied: bool
    positions: tuple[CriticalPaperTranche, ...]


@dataclass(frozen=True, slots=True)
class _CriticalSignal:
    base: _SignalInput
    original_text: str
    broad_order_type: str


@dataclass(frozen=True, slots=True)
class _Planned:
    local_id: UUID
    entry: CriticalEntry
    tp_index: int
    take_profit: Decimal | None
    client_id: str
    sizing: Day24RiskSizingResult


class PaperCriticalExecutionService(Day28GuardedExecutionService):
    """Owner DEMO executor for explicit pending and layered provider structures."""

    async def execute_owner_demo_signal(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        risk_percent,
        double_lot_approved: bool,
    ):
        critical = self._load_critical_signal(signal_id)
        try:
            entries = parse_critical_entries(
                critical.original_text,
                side=critical.base.side,
                entry_low=critical.base.entry_low,
                entry_high=critical.base.entry_high,
            )
        except ValueError as exc:
            raise Day26ExecutionError(str(exc)) from exc

        is_critical = critical.broad_order_type == "pending" or len(entries) > 1
        if not is_critical:
            return await super().execute_owner_demo_signal(
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )
        if not entries:
            raise Day26ExecutionError("critical_entry_plan_missing")
        return await self._execute_critical_demo(
            owner_user_id=owner_user_id,
            critical=critical,
            entries=entries,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )

    def _load_critical_signal(self, signal_id: UUID) -> _CriticalSignal:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, symbol, side, order_type, entry_low, entry_high,
                           stop_loss, take_profits, has_open_runner, parser_status,
                           risk_multiplier, source_revision_index, source_posted_at,
                           original_text
                    FROM signals
                    WHERE id=:signal_id
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
        if row is None:
            raise Day26ExecutionError("signal_not_found")
        if str(row["parser_status"] or "") != "accepted":
            raise Day26ExecutionError("signal_not_accepted")
        symbol = str(row["symbol"] or "").strip().upper()
        side = str(row["side"] or "").strip().upper()
        if symbol != "XAUUSD":
            raise Day26ExecutionError("day26_xauusd_required")
        if side not in {"BUY", "SELL"}:
            raise Day26ExecutionError("trade_side_invalid")
        low = self._required_decimal(row["entry_low"], "signal_entry_invalid")
        high = self._required_decimal(row["entry_high"], "signal_entry_invalid")
        stop = self._required_decimal(row["stop_loss"], "signal_stop_loss_invalid")
        tps = self._take_profits(row["take_profits"])
        posted_at = row["source_posted_at"]
        if not isinstance(posted_at, datetime):
            raise Day26ExecutionError("signal_posted_at_invalid")
        base = _SignalInput(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            entry_low=low,
            entry_high=high,
            stop_loss=stop,
            take_profits=tps,
            has_open_runner=bool(row["has_open_runner"]),
            signal_requests_double_lot=self._required_decimal(
                row["risk_multiplier"], "signal_risk_multiplier_invalid"
            ) > Decimal("1"),
            source_revision_index=int(row["source_revision_index"]),
            source_posted_at=posted_at,
        )
        if not self._directionally_valid(
            side=side,
            entry_low=low,
            entry_high=high,
            stop_loss=stop,
            take_profits=tps,
        ):
            raise Day26ExecutionError("strict_directional_validation_failed")
        return _CriticalSignal(
            base=base,
            original_text=str(row["original_text"] or ""),
            broad_order_type=str(row["order_type"] or "").strip().lower(),
        )

    def _load_demo_account(self, owner_user_id: UUID, signal_id: UUID) -> _AccountInput:
        with self._session_factory() as session:
            existing = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM positions WHERE signal_id=:signal_id AND user_id=:user_id"
                    ),
                    {"signal_id": signal_id, "user_id": owner_user_id},
                ).scalar_one()
            )
            if existing:
                raise Day26ExecutionError("signal_execution_already_started")
            cancelled = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM signal_lifecycle_events
                            WHERE signal_id=:signal_id AND event_type='cancel'
                        )
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalar_one()
            )
            if cancelled:
                raise Day26ExecutionError("signal_cancelled")
            row = session.execute(
                text(
                    """
                    SELECT id, metaapi_account_id, metaapi_token_ciphertext,
                           account_environment, status
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status!='revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": owner_user_id},
            ).mappings().first()
        if row is None:
            raise Day26ExecutionError("mt5_account_not_configured")
        if str(row["account_environment"] or "").lower() != "demo":
            raise Day26ExecutionError("paper_pending_demo_account_required")
        if str(row["status"] or "") != "connected":
            raise Day26ExecutionError("mt5_account_not_connected")
        return _AccountInput(
            local_account_id=UUID(str(row["id"])),
            metaapi_account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )

    async def _execute_critical_demo(
        self,
        *,
        owner_user_id: UUID,
        critical: _CriticalSignal,
        entries: tuple[CriticalEntry, ...],
        risk_percent,
        double_lot_approved: bool,
    ) -> CriticalPaperExecutionResult:
        signal = critical.base
        account = self._load_demo_account(owner_user_id, signal.signal_id)
        token = self._decrypt_token(account)
        day23 = Day23Mt5ReadService(
            session_factory=self._session_factory,
            cipher=self._cipher,
            gateway=self._read_gateway,
        )
        try:
            state = await day23.read_owner_live_state(owner_user_id)
            specification = await self._read_gateway.read_symbol_specification(
                token=token,
                account_id=account.metaapi_account_id,
                region=state.region,
                symbol=signal.symbol,
            )
        except Day23ReadError as exc:
            raise Day26ExecutionError(exc.code) from exc
        except MetaApiGatewayError as exc:
            raise Day26ExecutionError(exc.code) from exc
        if not state.account.trade_allowed:
            raise Day26ExecutionError("trading_not_allowed")

        try:
            current = Decimal(str(Day23Mt5ReadService.executable_price(state, signal.side)))
        except Day23ReadError as exc:
            raise Day26ExecutionError(exc.code) from exc

        self._validate_entry_timing(entries, signal.side, current)
        layer_count = Decimal(len(entries))
        layer_balance = Decimal(str(state.account.balance)) / layer_count
        sizings: dict[int, Day24RiskSizingResult] = {}
        for entry in entries:
            sizing_entry = current if entry.order_type == "market" else entry.price
            synthetic = _SignalInput(
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                side=signal.side,
                entry_low=sizing_entry,
                entry_high=sizing_entry,
                stop_loss=signal.stop_loss,
                take_profits=signal.take_profits,
                has_open_runner=signal.has_open_runner,
                signal_requests_double_lot=signal.signal_requests_double_lot,
                source_revision_index=signal.source_revision_index,
                source_posted_at=signal.source_posted_at,
            )
            sizings[entry.entry_index] = self._size_signal(
                signal=synthetic,
                execution_entry=sizing_entry,
                balance=float(layer_balance),
                price_loss_tick_value=state.price.loss_tick_value,
                specification=specification,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )

        self._assert_layer_risk_cap(
            real_balance=Decimal(str(state.account.balance)),
            risk_percent=Decimal(str(risk_percent)),
            double_applied=any(item.double_lot_applied for item in sizings.values()),
            sizings=tuple(sizings.values()),
            tp_count=signal.position_count,
        )
        await self._margin_preflight(
            token=token,
            account_id=account.metaapi_account_id,
            region=state.region,
            symbol=signal.symbol,
            side=signal.side,
            free_margin=Decimal(str(state.account.free_margin)),
            entries=entries,
            sizings=sizings,
        )
        self._assert_signal_still_current(owner_user_id, signal)
        planned = self._create_layered_plans(
            owner_user_id=owner_user_id,
            signal=signal,
            entries=entries,
            sizings=sizings,
            market_entry=current,
        )

        submitted: dict[UUID, str] = {}
        pending_gateway = PaperPendingOrderGateway(self._trade_gateway)
        try:
            # Broker-held pending layers first. The immediate market layer is last so
            # a rejected pending request cannot leave an uncovered market position.
            ordered = sorted(planned, key=lambda item: item.entry.order_type == "market")
            for item in ordered:
                if item.entry.order_type == "market":
                    result = await self._trade_gateway.place_market_order(
                        token=token,
                        account_id=account.metaapi_account_id,
                        region=state.region,
                        side=signal.side,
                        symbol=signal.symbol,
                        volume=float(item.sizing.volume),
                        stop_loss=float(signal.stop_loss),
                        take_profit=float(item.take_profit) if item.take_profit is not None else None,
                        client_id=item.client_id,
                    )
                else:
                    result = await pending_gateway.place_pending_order(
                        account_environment="demo",
                        token=token,
                        account_id=account.metaapi_account_id,
                        region=state.region,
                        request=PaperPendingOrderRequest(
                            order_type=item.entry.order_type,
                            symbol=signal.symbol,
                            volume=float(item.sizing.volume),
                            open_price=float(item.entry.price),
                            stop_loss=float(signal.stop_loss),
                            take_profit=float(item.take_profit) if item.take_profit is not None else None,
                            client_id=item.client_id,
                        ),
                    )
                submitted[item.local_id] = result.order_id
                self._record_critical_order(item, result.order_id, result.position_id)
        except MetaApiGatewayError as exc:
            complete = await self._rollback_critical(
                owner_user_id=owner_user_id,
                signal=signal,
                account=account,
                token=token,
                region=state.region,
                planned=planned,
                submitted=submitted,
                reason=exc.code,
            )
            raise Day26ExecutionError(
                exc.code if complete else "critical_partial_execution_rollback_failed"
            ) from exc

        mapped = await self._verify_critical_state(
            owner_user_id=owner_user_id,
            signal=signal,
            account=account,
            token=token,
            region=state.region,
            planned=planned,
            submitted=submitted,
            current_market_entry=current,
        )
        first_sizing = sizings[entries[0].entry_index]
        self._audit(
            owner_user_id=owner_user_id,
            signal_id=signal.signal_id,
            event_type="mt5.paper_critical_execution_success",
            payload={
                "entry_layers": len(entries),
                "pending_layers": sum(1 for item in entries if item.order_type != "market"),
                "tranche_count": len(mapped),
                "risk_split_across_layers": True,
                "paper_demo_only": True,
                "automatic_retry": False,
            },
        )
        return CriticalPaperExecutionResult(
            signal_id=signal.signal_id,
            user_id=owner_user_id,
            symbol=signal.symbol,
            side=signal.side,
            signal_entry_price=entries[0].price,
            stop_loss=signal.stop_loss,
            base_risk_percent=first_sizing.base_risk_percent,
            effective_risk_percent=first_sizing.effective_risk_percent,
            double_lot_applied=first_sizing.double_lot_applied,
            positions=mapped,
        )

    def _validate_entry_timing(
        self,
        entries: tuple[CriticalEntry, ...],
        side: str,
        current: Decimal,
    ) -> None:
        for entry in entries:
            if entry.order_type == "market":
                if abs(current - entry.price) > self._entry_tolerance:
                    raise Day26ExecutionError("layer_market_entry_no_longer_fresh")
                continue
            if entry.order_type == "buy_limit" and not entry.price < current:
                raise Day26ExecutionError("pending_entry_no_longer_valid")
            if entry.order_type == "buy_stop" and not entry.price > current:
                raise Day26ExecutionError("pending_entry_no_longer_valid")
            if entry.order_type == "sell_limit" and not entry.price > current:
                raise Day26ExecutionError("pending_entry_no_longer_valid")
            if entry.order_type == "sell_stop" and not entry.price < current:
                raise Day26ExecutionError("pending_entry_no_longer_valid")
            if side == "BUY" and not entry.order_type.startswith("buy_"):
                raise Day26ExecutionError("pending_side_mismatch")
            if side == "SELL" and not entry.order_type.startswith("sell_"):
                raise Day26ExecutionError("pending_side_mismatch")

    @staticmethod
    def _assert_layer_risk_cap(
        *,
        real_balance: Decimal,
        risk_percent: Decimal,
        double_applied: bool,
        sizings: tuple[Day24RiskSizingResult, ...],
        tp_count: int,
    ) -> None:
        multiplier = Decimal("2") if double_applied else Decimal("1")
        per_tp_cap = real_balance * risk_percent * multiplier / Decimal("100")
        actual_per_tp = sum((item.actual_risk_per_position for item in sizings), Decimal("0"))
        if actual_per_tp > per_tp_cap:
            raise Day26ExecutionError("layer_risk_budget_exceeded_by_broker_minimum")
        if tp_count <= 0:
            raise Day26ExecutionError("position_count_invalid")

    async def _margin_preflight(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        symbol: str,
        side: str,
        free_margin: Decimal,
        entries: tuple[CriticalEntry, ...],
        sizings: dict[int, Day24RiskSizingResult],
    ) -> None:
        required_total = Decimal("0")
        complete = True
        for entry in entries:
            sizing = sizings[entry.entry_index]
            total_volume = sizing.volume * Decimal(sizing.position_count)
            try:
                required = await self._margin_gateway.calculate_margin(
                    token=token,
                    account_id=account_id,
                    region=region,
                    symbol=symbol,
                    side=side,
                    volume=float(total_volume),
                    open_price=float(entry.price),
                )
                required_total += Decimal(str(required))
            except MetaApiGatewayError:
                complete = False
        if complete and required_total > free_margin:
            raise Day26ExecutionError("insufficient_funds")

    def _create_layered_plans(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        entries: tuple[CriticalEntry, ...],
        sizings: dict[int, Day24RiskSizingResult],
        market_entry: Decimal,
    ) -> tuple[_Planned, ...]:
        targets: list[Decimal | None] = list(signal.take_profits)
        if signal.has_open_runner:
            targets.append(None)
        planned: list[_Planned] = []
        layer_count = Decimal(len(entries))
        with self._session_factory() as session:
            for entry in entries:
                sizing = sizings[entry.entry_index]
                actual_risk_percent = sizing.effective_risk_percent / layer_count
                local_entry = market_entry if entry.order_type == "market" else entry.price
                for tp_index, take_profit in enumerate(targets, start=1):
                    local_id = uuid4()
                    client_id = f"SS_{local_id.hex[:12]}_E{entry.entry_index}T{tp_index}"
                    session.execute(
                        text(
                            """
                            INSERT INTO positions (
                                id, signal_id, user_id, entry_index, entry_order_type,
                                tp_index, take_profit, planned_risk_percent, volume,
                                stop_loss, broker_client_id, status, entry_price
                            ) VALUES (
                                :id, :signal_id, :user_id, :entry_index, :entry_order_type,
                                :tp_index, :take_profit, :risk_percent, :volume,
                                :stop_loss, :client_id, 'planned', :entry_price
                            )
                            """
                        ),
                        {
                            "id": local_id,
                            "signal_id": signal.signal_id,
                            "user_id": owner_user_id,
                            "entry_index": entry.entry_index,
                            "entry_order_type": entry.order_type,
                            "tp_index": tp_index,
                            "take_profit": take_profit,
                            "risk_percent": actual_risk_percent,
                            "volume": sizing.volume,
                            "stop_loss": signal.stop_loss,
                            "client_id": client_id,
                            "entry_price": local_entry,
                        },
                    )
                    planned.append(
                        _Planned(
                            local_id=local_id,
                            entry=entry,
                            tp_index=tp_index,
                            take_profit=take_profit,
                            client_id=client_id,
                            sizing=sizing,
                        )
                    )
            session.commit()
        return tuple(planned)

    def _record_critical_order(
        self,
        item: _Planned,
        order_id: str,
        position_id: str | None,
    ) -> None:
        status = "open" if position_id else "pending" if item.entry.order_type != "market" else "planned"
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_order_id=:order_id,
                        broker_position_id=COALESCE(:position_id, broker_position_id),
                        status=:status,
                        opened_at=CASE WHEN :status='open' THEN COALESCE(opened_at, now()) ELSE opened_at END,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": item.local_id,
                    "order_id": order_id,
                    "position_id": position_id,
                    "status": status,
                },
            )
            session.commit()

    async def _verify_critical_state(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        account: _AccountInput,
        token: str,
        region: str,
        planned: tuple[_Planned, ...],
        submitted: dict[UUID, str],
        current_market_entry: Decimal,
    ) -> tuple[CriticalPaperTranche, ...]:
        last_code = "critical_broker_mapping_missing"
        for attempt in range(_VERIFY_ATTEMPTS):
            try:
                positions = await self._read_gateway.read_positions(
                    token=token, account_id=account.metaapi_account_id, region=region
                )
                orders = await self._read_gateway.read_orders(
                    token=token, account_id=account.metaapi_account_id, region=region
                )
                by_client_position = {
                    str(row.get("clientId") or ""): row
                    for row in positions
                    if str(row.get("clientId") or "")
                }
                order_ids = {str(row.get("id") or "") for row in orders}
                result: list[CriticalPaperTranche] = []
                complete = True
                for item in planned:
                    order_id = submitted.get(item.local_id, "")
                    broker = by_client_position.get(item.client_id)
                    if broker is not None:
                        position_id = str(broker.get("id") or "").strip()
                        open_price = self._required_decimal(
                            broker.get("openPrice"), "broker_position_mapping_invalid"
                        )
                        if not position_id:
                            complete = False
                            break
                        self._persist_open_mapping(item.local_id, position_id, order_id, open_price)
                        status = "open"
                    elif item.entry.order_type != "market" and order_id in order_ids:
                        position_id = None
                        open_price = item.entry.price
                        self._persist_pending(item.local_id, order_id)
                        status = "pending"
                    else:
                        complete = False
                        break
                    result.append(
                        CriticalPaperTranche(
                            local_position_id=item.local_id,
                            entry_index=item.entry.entry_index,
                            tp_index=item.tp_index,
                            order_type=item.entry.order_type,
                            take_profit=item.take_profit,
                            volume=item.sizing.volume,
                            client_id=item.client_id,
                            broker_order_id=order_id,
                            broker_position_id=position_id,
                            entry_price=open_price,
                            status=status,
                        )
                    )
                if complete and len(result) == len(planned):
                    return tuple(result)
                last_code = "critical_broker_mapping_missing"
            except MetaApiGatewayError as exc:
                last_code = exc.code
                if not exc.retryable:
                    break
            if attempt + 1 < _VERIFY_ATTEMPTS:
                await asyncio.sleep(_VERIFY_DELAY_SECONDS)

        complete = await self._rollback_critical(
            owner_user_id=owner_user_id,
            signal=signal,
            account=account,
            token=token,
            region=region,
            planned=planned,
            submitted=submitted,
            reason=last_code,
        )
        raise Day26ExecutionError(
            last_code if complete else "critical_partial_execution_rollback_failed"
        )

    def _persist_open_mapping(
        self, local_id: UUID, position_id: str, order_id: str, open_price: Decimal
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_position_id=:position_id, broker_order_id=:order_id,
                        entry_price=:open_price, status='open',
                        opened_at=COALESCE(opened_at,:now), updated_at=:now
                    WHERE id=:id
                    """
                ),
                {
                    "id": local_id,
                    "position_id": position_id,
                    "order_id": order_id,
                    "open_price": open_price,
                    "now": now,
                },
            )
            session.commit()

    def _persist_pending(self, local_id: UUID, order_id: str) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    "UPDATE positions SET broker_order_id=:order_id, status='pending', updated_at=now() WHERE id=:id"
                ),
                {"id": local_id, "order_id": order_id},
            )
            session.commit()

    async def _rollback_critical(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        account: _AccountInput,
        token: str,
        region: str,
        planned: tuple[_Planned, ...],
        submitted: dict[UUID, str],
        reason: str,
    ) -> bool:
        unresolved = 0
        try:
            positions = await self._read_gateway.read_positions(
                token=token, account_id=account.metaapi_account_id, region=region
            )
            orders = await self._read_gateway.read_orders(
                token=token, account_id=account.metaapi_account_id, region=region
            )
        except MetaApiGatewayError:
            positions, orders = [], []
            unresolved = len(submitted)

        by_client = {
            str(row.get("clientId") or ""): str(row.get("id") or "").strip()
            for row in positions
            if str(row.get("clientId") or "")
        }
        active_orders = {str(row.get("id") or "") for row in orders}
        closed_ids: set[UUID] = set()
        cancelled_ids: set[UUID] = set()
        for item in reversed(planned):
            if item.local_id not in submitted:
                continue
            position_id = by_client.get(item.client_id, "")
            order_id = submitted[item.local_id]
            try:
                if position_id:
                    await self._trade_gateway.close_position(
                        token=token,
                        account_id=account.metaapi_account_id,
                        region=region,
                        position_id=position_id,
                    )
                    closed_ids.add(item.local_id)
                elif order_id in active_orders:
                    await self._trade_gateway.cancel_order(
                        token=token,
                        account_id=account.metaapi_account_id,
                        region=region,
                        order_id=order_id,
                    )
                    cancelled_ids.add(item.local_id)
                else:
                    unresolved += 1
            except MetaApiGatewayError:
                unresolved += 1

        now = datetime.now(UTC)
        with self._session_factory() as session:
            for item in planned:
                if item.local_id in closed_ids or item.local_id in cancelled_ids:
                    status = "closed"
                    close_reason = "critical_compensating_rollback"
                    closed_at = now
                elif item.local_id in submitted:
                    status = "error"
                    close_reason = f"critical_rollback_unresolved:{reason}"[:80]
                    closed_at = None
                else:
                    status = "error"
                    close_reason = f"critical_failed:{reason}"[:80]
                    closed_at = None
                session.execute(
                    text(
                        """
                        UPDATE positions
                        SET status=:status, close_reason=:close_reason,
                            closed_at=:closed_at, updated_at=:now
                        WHERE id=:id
                        """
                    ),
                    {
                        "id": item.local_id,
                        "status": status,
                        "close_reason": close_reason,
                        "closed_at": closed_at,
                        "now": now,
                    },
                )
            session.commit()
        self._audit(
            owner_user_id=owner_user_id,
            signal_id=signal.signal_id,
            event_type="mt5.paper_critical_compensating_rollback",
            payload={
                "submitted": len(submitted),
                "positions_closed": len(closed_ids),
                "orders_cancelled": len(cancelled_ids),
                "unresolved": unresolved,
                "paper_demo_only": True,
                "automatic_retry": False,
            },
        )
        return unresolved == 0


__all__ = ["CriticalPaperExecutionResult", "CriticalPaperTranche", "PaperCriticalExecutionService"]
