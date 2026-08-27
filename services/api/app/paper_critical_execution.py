"""Canonical explicit pending/layer execution shared by paper and future LIVE.

This service owns only broker structure that ordinary atomic MARKET execution cannot
represent: literal LIMIT/STOP orders and explicit multiple provider entry sections.

Trading-policy invariants:
* one atomic provider leg/section receives the selected risk percentage in full;
* risk is never divided across entry layers and no aggregate account-risk cap is applied;
* balance is used only to calculate what the selected risk percentage means;
* local free margin/capacity is never an execution veto; Vantage/MT5 is authoritative;
* pending orders stay broker-side at the provider's exact literal price and are never
  chased or converted to market;
* a fresh market layer uses the current executable quote and is not rejected merely
  because price moved away from the provider's earlier printed level;
* any ambiguous/partial broker mutation is reconciled by exact client/order/position ID
  before compensation and no trade POST is automatically retried.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text

from app.critical_entry_policy import CriticalEntry, parse_critical_entries
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_pending_gateway import MetaApiPendingOrderGateway, MetaApiPendingOrderRequest
from app.mt5_execution_day26 import Day26ExecutionError, _AccountInput, _SignalInput
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError
from app.risk_sizing_day24 import Day24RiskSizingResult
from app.provider_risk_policy import provider_tp_limit

_VERIFY_ATTEMPTS = 3
_VERIFY_DELAY_SECONDS = 0.25
_AMBIGUOUS_BROKER_CODES = {
    "metaapi_timeout",
    "metaapi_unreachable",
    "metaapi_temporarily_unavailable",
}
_RECONCILE_ATTEMPTS = 3
_RECONCILE_DELAY_SECONDS = 0.35


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


@dataclass(frozen=True, slots=True)
class AtomicLayerAllocation:
    entry: CriticalEntry
    tp_index: int
    take_profit: Decimal | None


class PaperCriticalExecutionService(AtomicDay26Mt5ExecutionService):
    """Shared executor for explicit pending and multiple-entry provider structures."""

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
                    SELECT sig.id,sig.symbol,sig.side,sig.order_type,
                           sig.entry_low,sig.entry_high,sig.stop_loss,
                           sig.take_profits,sig.has_open_runner,sig.parser_status,
                           sig.risk_multiplier,sig.source_revision_index,
                           sig.source_posted_at,sig.original_text,
                           COALESCE(src.source_alias,src.chat_title,'') AS source_name
                    FROM signals sig
                    LEFT JOIN sources src ON src.id=sig.source_id
                    WHERE sig.id=:signal_id
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
        has_open_runner = bool(row["has_open_runner"])
        tp_limit = provider_tp_limit(
            source_name=str(row["source_name"] or ""), side=side
        )
        if tp_limit is not None:
            tps = tps[:tp_limit]
            has_open_runner = False
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
            has_open_runner=has_open_runner,
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
                    SELECT id,metaapi_account_id,metaapi_token_ciphertext,
                           account_environment,status
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

        self._validate_entry_structure(entries, signal.side)
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
                balance=state.account.balance,
                price_loss_tick_value=state.price.loss_tick_value,
                specification=specification,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )

        # No local balance/free-margin/capacity approval step exists here. A valid
        # provider leg is sent to the broker; broker rejection is preserved as truth.
        self._assert_signal_still_current(owner_user_id, signal)
        planned = self._create_layered_plans(
            owner_user_id=owner_user_id,
            signal=signal,
            entries=entries,
            sizings=sizings,
            market_entry=current,
        )

        submitted: dict[UUID, str] = {}
        pending_gateway = MetaApiPendingOrderGateway(self._trade_gateway)
        try:
            # Broker-held pending legs first. Immediate market legs are last so a
            # rejected pending request cannot leave only part of the requested setup.
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
                        take_profit=(
                            float(item.take_profit) if item.take_profit is not None else None
                        ),
                        client_id=item.client_id,
                    )
                else:
                    result = await pending_gateway.place_pending_order(
                        token=token,
                        account_id=account.metaapi_account_id,
                        region=state.region,
                        request=MetaApiPendingOrderRequest(
                            order_type=item.entry.order_type,
                            symbol=signal.symbol,
                            volume=float(item.sizing.volume),
                            open_price=float(item.entry.price),
                            stop_loss=float(signal.stop_loss),
                            take_profit=(
                                float(item.take_profit) if item.take_profit is not None else None
                            ),
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
                "entry_sections": len(entries),
                "pending_sections": sum(1 for item in entries if item.order_type != "market"),
                "atomic_leg_count": len(mapped),
                "risk_per_atomic_leg": True,
                "risk_split_across_layers": False,
                "local_margin_veto": False,
                "broker_margin_authority": True,
                "automatic_retry": False,
            },
        )
        return CriticalPaperExecutionResult(
            signal_id=signal.signal_id,
            user_id=owner_user_id,
            symbol=signal.symbol,
            side=signal.side,
            signal_entry_price=current,
            stop_loss=signal.stop_loss,
            base_risk_percent=first_sizing.base_risk_percent,
            effective_risk_percent=first_sizing.effective_risk_percent,
            double_lot_applied=first_sizing.double_lot_applied,
            positions=mapped,
        )

    @staticmethod
    def _validate_entry_structure(entries: tuple[CriticalEntry, ...], side: str) -> None:
        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise Day26ExecutionError("trade_side_invalid")
        for entry in entries:
            order_type = entry.order_type.strip().lower()
            if order_type == "market":
                continue
            if normalized_side == "BUY" and order_type not in {"buy_limit", "buy_stop"}:
                raise Day26ExecutionError("pending_side_mismatch")
            if normalized_side == "SELL" and order_type not in {"sell_limit", "sell_stop"}:
                raise Day26ExecutionError("pending_side_mismatch")

    @staticmethod
    def _allocation_pairs(
        entries: tuple[CriticalEntry, ...],
        targets: tuple[Decimal | None, ...],
    ) -> tuple[AtomicLayerAllocation, ...]:
        if not entries:
            raise Day26ExecutionError("critical_entry_plan_missing")
        if not targets:
            raise Day26ExecutionError("position_count_invalid")
        slot_count = max(len(entries), len(targets))
        has_runner = targets[-1] is None
        target_indexes = list(range(1, len(targets) + 1))
        if slot_count > len(targets):
            repeatable = list(range(1, len(targets) if has_runner else len(targets) + 1))
            if not repeatable:
                raise Day26ExecutionError("position_count_invalid")
            for offset in range(slot_count - len(targets)):
                target_indexes.append(repeatable[offset % len(repeatable)])
        allocations = [
            AtomicLayerAllocation(
                entry=entries[index % len(entries)],
                tp_index=target_index,
                take_profit=targets[target_index - 1],
            )
            for index, target_index in enumerate(target_indexes)
        ]
        if has_runner:
            runner_slot = next(
                index for index, item in enumerate(allocations) if item.take_profit is None
            )
            best_entry = entries[-1]
            if allocations[runner_slot].entry.entry_index != best_entry.entry_index:
                best_slot = next(
                    index
                    for index, item in enumerate(allocations)
                    if item.entry.entry_index == best_entry.entry_index
                    and item.take_profit is not None
                )
                runner_item = allocations[runner_slot]
                best_item = allocations[best_slot]
                allocations[runner_slot] = AtomicLayerAllocation(
                    entry=best_item.entry,
                    tp_index=runner_item.tp_index,
                    take_profit=runner_item.take_profit,
                )
                allocations[best_slot] = AtomicLayerAllocation(
                    entry=runner_item.entry,
                    tp_index=best_item.tp_index,
                    take_profit=best_item.take_profit,
                )
        return tuple(allocations)

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
        allocations = self._allocation_pairs(entries, tuple(targets))
        planned: list[_Planned] = []
        with self._session_factory() as session:
            for allocation in allocations:
                entry = allocation.entry
                base_sizing = sizings[entry.entry_index]
                try:
                    leg = base_sizing.positions[allocation.tp_index - 1]
                except IndexError as exc:
                    raise Day26ExecutionError("risk_profile_position_missing") from exc
                sizing = replace(
                    base_sizing,
                    effective_risk_percent=(
                        leg.risk_budget * Decimal("100") / base_sizing.balance
                    ),
                    raw_volume=leg.volume,
                    volume=leg.volume,
                    risk_budget_per_position=leg.risk_budget,
                    actual_risk_per_position=leg.actual_risk,
                    position_count=1,
                    total_risk_budget=leg.risk_budget,
                    total_actual_risk=leg.actual_risk,
                    positions=(leg,),
                )
                local_id = uuid4()
                client_id = f"SS_{local_id.hex[:12]}_E{entry.entry_index}T{allocation.tp_index}"
                local_entry = market_entry if entry.order_type == "market" else entry.price
                session.execute(
                    text(
                        """
                        INSERT INTO positions (
                            id,signal_id,user_id,entry_index,entry_order_type,
                            tp_index,take_profit,planned_risk_percent,volume,
                            stop_loss,broker_client_id,status,entry_price
                        ) VALUES (
                            :id,:signal_id,:user_id,:entry_index,:entry_order_type,
                            :tp_index,:take_profit,:risk_percent,:volume,
                            :stop_loss,:client_id,'planned',:entry_price
                        )
                        """
                    ),
                    {
                        "id": local_id,
                        "signal_id": signal.signal_id,
                        "user_id": owner_user_id,
                        "entry_index": entry.entry_index,
                        "entry_order_type": entry.order_type,
                        "tp_index": allocation.tp_index,
                        "take_profit": allocation.take_profit,
                        "risk_percent": sizing.effective_risk_percent,
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
                        tp_index=allocation.tp_index,
                        take_profit=allocation.take_profit,
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
        status = (
            "open"
            if position_id
            else "pending"
            if item.entry.order_type != "market"
            else "planned"
        )
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_order_id=:order_id,
                        broker_position_id=COALESCE(:position_id,broker_position_id),
                        status=:status,
                        opened_at=CASE WHEN :is_open
                                       THEN COALESCE(opened_at,now())
                                       ELSE opened_at END,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": item.local_id,
                    "order_id": order_id,
                    "position_id": position_id,
                    "status": status,
                    "is_open": status == "open",
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
        del current_market_entry
        last_code = "critical_broker_mapping_missing"
        for attempt in range(_VERIFY_ATTEMPTS):
            try:
                positions = await self._read_gateway.read_positions(
                    token=token,
                    account_id=account.metaapi_account_id,
                    region=region,
                )
                orders = await self._read_gateway.read_orders(
                    token=token,
                    account_id=account.metaapi_account_id,
                    region=region,
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
        self,
        local_id: UUID,
        position_id: str,
        order_id: str,
        open_price: Decimal,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_position_id=:position_id,broker_order_id=:order_id,
                        entry_price=:open_price,status='open',
                        opened_at=COALESCE(opened_at,:now),updated_at=:now
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
                    """
                    UPDATE positions
                    SET broker_order_id=:order_id,status='pending',updated_at=now()
                    WHERE id=:id
                    """
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
        """Reconcile every planned client ID before exact-ID compensation."""
        attempts = _RECONCILE_ATTEMPTS if reason in _AMBIGUOUS_BROKER_CODES else 1
        positions: list[dict[str, object]] = []
        orders: list[dict[str, object]] = []
        read_failed = False
        for attempt in range(attempts):
            try:
                positions = await self._read_gateway.read_positions(
                    token=token,
                    account_id=account.metaapi_account_id,
                    region=region,
                )
                orders = await self._read_gateway.read_orders(
                    token=token,
                    account_id=account.metaapi_account_id,
                    region=region,
                )
                read_failed = False
                break
            except MetaApiGatewayError:
                read_failed = True
                if attempt + 1 < attempts:
                    await asyncio.sleep(_RECONCILE_DELAY_SECONDS)

        by_client_position = {
            str(row.get("clientId") or ""): str(row.get("id") or "").strip()
            for row in positions
            if str(row.get("clientId") or "") and str(row.get("id") or "").strip()
        }
        by_client_order = {
            str(row.get("clientId") or ""): str(row.get("id") or "").strip()
            for row in orders
            if str(row.get("clientId") or "") and str(row.get("id") or "").strip()
        }
        active_order_ids = {
            str(row.get("id") or "").strip()
            for row in orders
            if str(row.get("id") or "").strip()
        }

        closed_ids: set[UUID] = set()
        cancelled_ids: set[UUID] = set()
        hidden_detected: set[UUID] = set()
        unresolved_ids: set[UUID] = set()
        for item in reversed(planned):
            returned_order_id = str(submitted.get(item.local_id, "") or "").strip()
            position_id = by_client_position.get(item.client_id, "")
            discovered_order_id = by_client_order.get(item.client_id, "")
            order_id = returned_order_id or discovered_order_id

            if item.local_id not in submitted and (position_id or discovered_order_id):
                hidden_detected.add(item.local_id)
            if not position_id and not order_id:
                if read_failed and item.local_id in submitted:
                    unresolved_ids.add(item.local_id)
                continue

            try:
                if position_id:
                    await self._trade_gateway.close_position(
                        token=token,
                        account_id=account.metaapi_account_id,
                        region=region,
                        position_id=position_id,
                    )
                    closed_ids.add(item.local_id)
                elif order_id in active_order_ids:
                    await self._trade_gateway.cancel_order(
                        token=token,
                        account_id=account.metaapi_account_id,
                        region=region,
                        order_id=order_id,
                    )
                    cancelled_ids.add(item.local_id)
                elif item.local_id in submitted or item.local_id in hidden_detected:
                    unresolved_ids.add(item.local_id)
            except MetaApiGatewayError:
                unresolved_ids.add(item.local_id)

        now = datetime.now(UTC)
        with self._session_factory() as session:
            for item in planned:
                cleaned = item.local_id in closed_ids or item.local_id in cancelled_ids
                known_mutation = item.local_id in submitted or item.local_id in hidden_detected
                if cleaned:
                    status = "closed"
                    close_reason = "critical_compensating_rollback"
                    closed_at = now
                elif item.local_id in unresolved_ids or known_mutation:
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
                        SET status=:status,close_reason=:close_reason,
                            closed_at=:closed_at,updated_at=:now
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
                "hidden_mutations_detected": len(hidden_detected),
                "unresolved": len(unresolved_ids),
                "ambiguous_mutation_reconciled": reason in _AMBIGUOUS_BROKER_CODES,
                "automatic_retry": False,
            },
        )
        return not unresolved_ids


__all__ = [
    "AtomicLayerAllocation",
    "CriticalPaperExecutionResult",
    "CriticalPaperTranche",
    "PaperCriticalExecutionService",
    "_CriticalSignal",
    "_Planned",
]
