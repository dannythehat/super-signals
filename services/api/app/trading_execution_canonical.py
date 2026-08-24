"""Canonical shared paper/future-LIVE trading execution.

Provider entry layers and TP/runner targets are represented with the minimum faithful set
of atomic broker positions. Risk is per provider section: selected 1% means every
leg/section carries 1% (or the explicitly approved effective risk), never an aggregate
account pot and never divided across the signal.

There is deliberately no local balance/free-margin/capacity veto. The deterministic risk
sizer may use fresh broker balance to calculate the monetary amount represented by the
selected percentage, but local code does not decide whether the account can afford the
requested provider trade. Every valid broker mutation is submitted; Vantage/MT5 is the
sole authority for an actual funds/margin rejection.

The exact standalone BUY/SELL GOLD/XAUUSD NOW profile is the only trade allowed to have
neither provider SL nor TP. Its canonical Signal remains NULL for those fields. This
request-local executor derives the paper-tested 50-pip TP and 100-pip SL from the fresh
broker executable quote immediately before sizing/submission.

An ambiguous MetaAPI POST failure is never retried. Compensation reconciles every
planned client ID against broker positions/orders and closes/cancels only exact broker
artifacts that actually exist.

Paper and future LIVE use this exact policy engine. The member adapter below changes only
account/user eligibility and credential selection; the outer distribution switch decides
whether ordinary LIVE member mutation is enabled.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text

from app.bare_gold_now_policy import (
    STOP_LOSS_DISTANCE,
    TAKE_PROFIT_DISTANCE,
    bare_now_side,
)
from app.critical_entry_policy import CriticalEntry, parse_critical_entries
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    Day26ExecutionResult,
    Day26Mt5ExecutionService,
    _AccountInput,
    _SignalInput,
)
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.mt5_read_service_day23 import Day23LiveState, Day23Mt5ReadService, Day23ReadError
from app.paper_critical_execution import PaperCriticalExecutionService, _Planned
from app.paper_execution_priority import PaperExecutionPriorityService
from app.paper_resilient_read_gateway import ResilientMetaApiReadGateway
from app.risk_sizing_day24 import Day24RiskSizingResult


_AMBIGUOUS_BROKER_CODES = {
    "metaapi_timeout",
    "metaapi_unreachable",
    "metaapi_temporarily_unavailable",
}
_RECONCILE_ATTEMPTS = 3
_RECONCILE_DELAY_SECONDS = 0.35


@dataclass(frozen=True, slots=True)
class AtomicLayerAllocation:
    entry: CriticalEntry
    tp_index: int
    take_profit: Decimal | None


class CanonicalTradingExecutionService(PaperExecutionPriorityService):
    """Shared executor with full selected risk per provider section."""

    async def execute_owner_demo_signal(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        risk_percent,
        double_lot_approved: bool,
    ):
        """Route one canonical signal by literal provider broker structure."""
        with self._session_factory() as session:
            shape = session.execute(
                text(
                    """
                    SELECT order_type,entry_low,entry_high
                    FROM signals WHERE id=:signal_id LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
        if shape is None:
            raise Day26ExecutionError("signal_not_found")

        no_entry_market = (
            str(shape["order_type"] or "").lower() == "market"
            and shape["entry_low"] is None
            and shape["entry_high"] is None
        )
        if no_entry_market:
            entries: tuple[CriticalEntry, ...] = ()
            is_critical = False
        else:
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

        if is_critical:
            return await PaperCriticalExecutionService.execute_owner_demo_signal(
                self,
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )
        return await AtomicDay26Mt5ExecutionService.execute_owner_demo_signal(
            self,
            owner_user_id=owner_user_id,
            signal_id=signal_id,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )

    def _load_inputs(self, owner_user_id: UUID, signal_id: UUID):
        bare = self._load_bare_now_signal(signal_id)
        if bare is not None:
            return bare, self._load_demo_account(owner_user_id, signal_id)
        return super()._load_inputs(owner_user_id, signal_id)

    def _load_bare_now_signal(self, signal_id: UUID) -> _SignalInput | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT symbol,side,order_type,entry_low,entry_high,stop_loss,
                           take_profits,has_open_runner,parser_status,risk_multiplier,
                           source_revision_index,source_posted_at,original_text
                    FROM signals
                    WHERE id=:signal_id
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
        if row is None:
            return None

        literal_side = bare_now_side(str(row["original_text"] or ""))
        if literal_side is None:
            return None
        targets = row["take_profits"] if isinstance(row["take_profits"], (list, tuple)) else []
        if (
            str(row["parser_status"] or "") != "accepted"
            or str(row["symbol"] or "").strip().upper() != "XAUUSD"
            or str(row["side"] or "").strip().upper() != literal_side
            or str(row["order_type"] or "").strip().lower() != "market"
            or row["entry_low"] is not None
            or row["entry_high"] is not None
            or row["stop_loss"] is not None
            or targets
            or bool(row["has_open_runner"])
        ):
            raise Day26ExecutionError("bare_gold_now_profile_invalid")
        posted_at = row["source_posted_at"]
        if not isinstance(posted_at, datetime):
            raise Day26ExecutionError("signal_posted_at_invalid")
        risk_multiplier = Day26Mt5ExecutionService._required_decimal(
            row["risk_multiplier"], "signal_risk_multiplier_invalid"
        )
        if risk_multiplier != Decimal("1"):
            raise Day26ExecutionError("bare_gold_now_profile_invalid")
        return _SignalInput(
            signal_id=signal_id,
            symbol="XAUUSD",
            side=literal_side,
            entry_low=Decimal("0"),
            entry_high=Decimal("0"),
            stop_loss=Decimal("0"),
            take_profits=(),
            has_open_runner=False,
            signal_requests_double_lot=False,
            source_revision_index=int(row["source_revision_index"]),
            source_posted_at=posted_at,
        )

    async def _resolve_entry(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        day23: Day23Mt5ReadService,
        initial_state: Day23LiveState,
    ) -> tuple[Decimal, Day23LiveState]:
        if (
            signal.entry_low == 0
            and signal.entry_high == 0
            and signal.stop_loss == 0
            and not signal.take_profits
        ):
            self._assert_signal_recent(signal)
            with self._session_factory() as session:
                raw_text = session.execute(
                    text("SELECT original_text FROM signals WHERE id=:signal_id LIMIT 1"),
                    {"signal_id": signal.signal_id},
                ).scalar_one_or_none()
            literal_side = bare_now_side(str(raw_text or ""))
            if literal_side is None or literal_side != signal.side:
                raise Day26ExecutionError("bare_gold_now_profile_invalid")
            try:
                executable = Decimal(
                    str(Day23Mt5ReadService.executable_price(initial_state, signal.side))
                )
            except Day23ReadError as exc:
                raise Day26ExecutionError(exc.code) from exc
            if signal.side == "BUY":
                stop_loss = executable - STOP_LOSS_DISTANCE
                take_profit = executable + TAKE_PROFIT_DISTANCE
            else:
                stop_loss = executable + STOP_LOSS_DISTANCE
                take_profit = executable - TAKE_PROFIT_DISTANCE
            if stop_loss <= 0 or take_profit <= 0:
                raise Day26ExecutionError("bare_gold_now_protection_invalid")
            object.__setattr__(signal, "stop_loss", stop_loss)
            object.__setattr__(signal, "take_profits", (take_profit,))
            return executable, initial_state
        return await super()._resolve_entry(
            owner_user_id=owner_user_id,
            signal=signal,
            day23=day23,
            initial_state=initial_state,
        )

    @staticmethod
    def _required_decimal(value: object, code: str) -> Decimal:
        if code == "signal_entry_invalid" and value is None:
            return Decimal("0")
        return Day26Mt5ExecutionService._required_decimal(value, code)

    @staticmethod
    def _directionally_valid(
        *,
        side: str,
        entry_low: Decimal,
        entry_high: Decimal,
        stop_loss: Decimal,
        take_profits: tuple[Decimal, ...],
    ) -> bool:
        if entry_low == 0 and entry_high == 0:
            if stop_loss <= 0 or not take_profits:
                return False
            if side == "BUY":
                return all(right > left for left, right in zip(take_profits, take_profits[1:]))
            if side == "SELL":
                return all(right < left for left, right in zip(take_profits, take_profits[1:]))
            return False
        return Day26Mt5ExecutionService._directionally_valid(
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
        )

    def _provider_zone(self, signal_id: UUID) -> tuple[Decimal, Decimal]:
        with self._session_factory() as session:
            row = session.execute(
                text("SELECT entry_low,entry_high FROM signals WHERE id=:signal_id LIMIT 1"),
                {"signal_id": signal_id},
            ).mappings().first()
        if row is not None and row["entry_low"] is None and row["entry_high"] is None:
            return Decimal("0"), Decimal("0")
        return super()._provider_zone(signal_id)

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
        """Validate local shape only; broker funds/margin authority is MT5 itself."""
        del token, account_id, region, symbol, side, free_margin, entries
        if not sizings:
            raise Day26ExecutionError("position_count_invalid")
        target_count = int(next(iter(sizings.values())).position_count)
        if target_count <= 0:
            raise Day26ExecutionError("position_count_invalid")

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

    def _create_layered_plans(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        entries: tuple[CriticalEntry, ...],
        allocations: tuple[AtomicLayerAllocation, ...],
        sizings: dict[tuple[int, int], Day24RiskSizingResult],
        market_entry: Decimal,
    ) -> tuple[_Planned, ...]:
        planned: list[_Planned] = []
        with self._session_factory() as session:
            for allocation in allocations:
                entry = allocation.entry
                sizing = sizings[(entry.entry_index, allocation.tp_index)]
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

    async def _rollback_critical(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        account,
        token: str,
        region: str,
        planned: tuple[_Planned, ...],
        submitted: dict[UUID, str],
        reason: str,
    ) -> bool:
        """Reconcile all planned client IDs before exact-ID compensation."""
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
                known_broker_mutation = item.local_id in submitted or item.local_id in hidden_detected
                if cleaned:
                    status = "closed"
                    close_reason = "critical_compensating_rollback"
                    closed_at = now
                elif item.local_id in unresolved_ids or known_broker_mutation:
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
        return len(unresolved_ids) == 0


class MemberTradingExecutionService(CanonicalTradingExecutionService):
    """Run the canonical trading engine against one approved member LIVE account."""

    def __init__(self, **kwargs) -> None:
        read_gateway = kwargs.get("read_gateway")
        if type(read_gateway) is MetaApiReadGateway:
            kwargs["read_gateway"] = ResilientMetaApiReadGateway()
        super().__init__(**kwargs)

    async def execute_live_user_signal(
        self,
        *,
        user_id: UUID,
        signal_id: UUID,
    ) -> Day26ExecutionResult:
        risk_percent, allow_double_lot = self._load_live_preferences(user_id)
        return await self.execute_owner_demo_signal(
            owner_user_id=user_id,
            signal_id=signal_id,
            risk_percent=risk_percent,
            double_lot_approved=allow_double_lot,
        )

    def _load_live_preferences(self, user_id: UUID) -> tuple[str, bool]:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT utc.risk_percent, utc.allow_double_lot
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN user_trading_controls AS utc ON utc.user_id=u.id
                    WHERE u.id=:user_id
                      AND u.status='active'
                      AND utc.trading_status='active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            raise Day26ExecutionError("day38_user_not_execution_active")
        return str(row["risk_percent"]), bool(row["allow_double_lot"])

    def _load_inputs(self, user_id: UUID, signal_id: UUID) -> tuple[_SignalInput, _AccountInput]:
        bare = self._load_bare_now_signal(signal_id)
        if bare is not None:
            return bare, self._load_demo_account(user_id, signal_id)

        with self._session_factory() as session:
            signal_row = session.execute(
                text(
                    """
                    SELECT id, symbol, side, order_type, entry_low, entry_high,
                           stop_loss, take_profits, has_open_runner, parser_status,
                           risk_multiplier, source_revision_index, source_posted_at
                    FROM signals
                    WHERE id=:signal_id
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if signal_row is None:
                raise Day26ExecutionError("signal_not_found")
            if str(signal_row["parser_status"] or "") != "accepted":
                raise Day26ExecutionError("signal_not_accepted")
            if str(signal_row["order_type"] or "").lower() != "market":
                raise Day26ExecutionError("day26_market_signal_required")

            existing_positions = int(
                session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM positions
                        WHERE signal_id=:signal_id AND user_id=:user_id
                        """
                    ),
                    {"signal_id": signal_id, "user_id": user_id},
                ).scalar_one()
            )
            if existing_positions > 0:
                raise Day26ExecutionError("signal_execution_already_started")

            cancelled = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM signal_lifecycle_events
                            WHERE signal_id=:signal_id
                              AND event_type='cancel'
                        )
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalar_one()
            )
            if cancelled:
                raise Day26ExecutionError("signal_cancelled")

            eligibility = self._live_account_row(session, user_id)
            if eligibility is None:
                raise Day26ExecutionError("day38_user_not_execution_ready")
            self._validate_live_account_row(eligibility)

        symbol = str(signal_row["symbol"] or "").strip().upper()
        side = str(signal_row["side"] or "").strip().upper()
        if symbol != "XAUUSD":
            raise Day26ExecutionError("day26_xauusd_required")
        if side not in {"BUY", "SELL"}:
            raise Day26ExecutionError("trade_side_invalid")

        entry_low = self._required_decimal(signal_row["entry_low"], "signal_entry_invalid")
        entry_high = self._required_decimal(signal_row["entry_high"], "signal_entry_invalid")
        if entry_high < entry_low:
            raise Day26ExecutionError("signal_entry_invalid")
        stop_loss = self._required_decimal(signal_row["stop_loss"], "signal_stop_loss_invalid")
        take_profits = self._take_profits(signal_row["take_profits"])
        has_open_runner = bool(signal_row["has_open_runner"])
        if not self._directionally_valid(
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
        ):
            raise Day26ExecutionError("strict_directional_validation_failed")

        risk_multiplier = self._required_decimal(
            signal_row["risk_multiplier"], "signal_risk_multiplier_invalid"
        )
        source_posted_at = signal_row["source_posted_at"]
        if source_posted_at is None:
            raise Day26ExecutionError("signal_posted_at_invalid")

        signal = _SignalInput(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
            has_open_runner=has_open_runner,
            signal_requests_double_lot=risk_multiplier > Decimal("1"),
            source_revision_index=int(signal_row["source_revision_index"]),
            source_posted_at=source_posted_at,
        )
        account = _AccountInput(
            local_account_id=UUID(str(eligibility["mt5_account_id"])),
            metaapi_account_id=str(eligibility["metaapi_account_id"]),
            token_ciphertext=bytes(eligibility["metaapi_token_ciphertext"]),
        )
        return signal, account

    def _load_demo_account(self, user_id: UUID, signal_id: UUID) -> _AccountInput:
        """Shared-engine account adapter: select the approved member LIVE account."""
        with self._session_factory() as session:
            existing = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM positions WHERE signal_id=:signal_id AND user_id=:user_id"
                    ),
                    {"signal_id": signal_id, "user_id": user_id},
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
            row = self._live_account_row(session, user_id)
        if row is None:
            raise Day26ExecutionError("day38_user_not_execution_ready")
        self._validate_live_account_row(row)
        return _AccountInput(
            local_account_id=UUID(str(row["mt5_account_id"])),
            metaapi_account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )

    @staticmethod
    def _live_account_row(session, user_id: UUID):  # noqa: ANN001
        return session.execute(
            text(
                """
                SELECT
                    m.id AS mt5_account_id,
                    m.metaapi_account_id,
                    m.metaapi_token_ciphertext,
                    m.account_environment,
                    m.login,
                    m.server,
                    a.login AS approved_login,
                    a.server AS approved_server
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id=u.id
                JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                JOIN user_trading_controls AS utc ON utc.user_id=u.id
                JOIN mt5_accounts AS m ON m.owner_user_id=u.id
                JOIN mt5_account_approvals AS a
                  ON a.user_id=u.id
                 AND a.status='active'
                WHERE u.id=:user_id
                  AND u.status='active'
                  AND utc.trading_status='active'
                  AND m.status!='revoked'
                ORDER BY m.created_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        ).mappings().first()

    def _rollback_account(self, user_id: UUID) -> tuple[str, str]:
        with self._session_factory() as session:
            row = self._live_account_row(session, user_id)
        if row is None:
            raise Day26ExecutionError("day38_user_not_execution_ready")
        self._validate_live_account_row(row)
        try:
            token = self._cipher.decrypt(bytes(row["metaapi_token_ciphertext"])).strip()
        except BrokerCredentialDecryptionError as exc:
            raise Day26ExecutionError("broker_credential_decryption_failed") from exc
        if len(token) < 20:
            raise Day26ExecutionError("metaapi_platform_token_not_configured")
        return str(row["metaapi_account_id"]), token

    @staticmethod
    def _validate_live_account_row(row) -> None:  # noqa: ANN001
        if str(row["account_environment"] or "").lower() != "live":
            raise Day26ExecutionError("day38_live_account_required")
        if str(row["login"] or "") != str(row["approved_login"] or ""):
            raise Day26ExecutionError("mt5_account_not_approved")
        if str(row["server"] or "").strip().lower() != str(
            row["approved_server"] or ""
        ).strip().lower():
            raise Day26ExecutionError("mt5_account_not_approved")


__all__ = [
    "AtomicLayerAllocation",
    "CanonicalTradingExecutionService",
    "MemberTradingExecutionService",
]
