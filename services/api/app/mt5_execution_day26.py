"""Day 26 fail-closed XAUUSD demo execution for exact and simple-zone signals.

The input is an already-canonical V1 Signal. This service reuses Day 23 broker state,
Day 24 risk sizing and the Day 25 exact-price/funds preflight, then submits one market
position per numeric provider TP plus an optional TP OPEN runner.

V1 deliberately does not place pending orders, chase a zone edge, choose a midpoint,
use TIG's second entry, or reinterpret missing provider values.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher
from app.mt5_read_service_day23 import (
    Day23LiveState,
    Day23Mt5ReadService,
    Day23ReadError,
)
from app.risk_sizing_day24 import (
    BrokerVolumeRules,
    Day24RiskSizer,
    Day24RiskSizingError,
    Day24RiskSizingResult,
)
from app.trade_preflight_day25 import Day25TradePreflightService

logger = logging.getLogger(__name__)


class Day26ExecutionError(RuntimeError):
    """Sanitized Day 26 execution failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


"""Maximum distance between a provider's stated single-price entry and the live
executable price that may still be filled, expressed in the instrument's price
units. 0.50 on XAUUSD is roughly five pips. Set to 0 to restore strict equality."""
DEFAULT_ENTRY_TOLERANCE = Decimal("0.50")


def _resolve_entry_tolerance(value: Decimal | str | None) -> Decimal:
    raw = value if value is not None else os.getenv("SUPER_SIGNALS_ENTRY_TOLERANCE")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_ENTRY_TOLERANCE
    try:
        parsed = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        logger.error(
            "Invalid SUPER_SIGNALS_ENTRY_TOLERANCE; using %s", DEFAULT_ENTRY_TOLERANCE
        )
        return DEFAULT_ENTRY_TOLERANCE
    if not parsed.is_finite() or parsed < 0:
        logger.error(
            "SUPER_SIGNALS_ENTRY_TOLERANCE must be >= 0; using %s",
            DEFAULT_ENTRY_TOLERANCE,
        )
        return DEFAULT_ENTRY_TOLERANCE
    return parsed


@dataclass(frozen=True, slots=True)
class Day26MappedPosition:
    local_position_id: UUID
    tp_index: int
    take_profit: Decimal | None
    volume: Decimal
    client_id: str
    broker_order_id: str
    broker_position_id: str
    broker_open_price: Decimal


@dataclass(frozen=True, slots=True)
class Day26ExecutionResult:
    signal_id: UUID
    user_id: UUID
    symbol: str
    side: str
    signal_entry_price: Decimal
    stop_loss: Decimal
    base_risk_percent: Decimal
    effective_risk_percent: Decimal
    double_lot_applied: bool
    positions: tuple[Day26MappedPosition, ...]


@dataclass(frozen=True, slots=True)
class _SignalInput:
    signal_id: UUID
    symbol: str
    side: str
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    take_profits: tuple[Decimal, ...]
    has_open_runner: bool
    signal_requests_double_lot: bool
    source_revision_index: int
    source_posted_at: datetime

    @property
    def is_zone(self) -> bool:
        return self.entry_low != self.entry_high

    @property
    def position_count(self) -> int:
        return len(self.take_profits) + (1 if self.has_open_runner else 0)


@dataclass(frozen=True, slots=True)
class _AccountInput:
    local_account_id: UUID
    metaapi_account_id: str
    token_ciphertext: bytes


@dataclass(frozen=True, slots=True)
class _PlannedPosition:
    local_position_id: UUID
    tp_index: int
    take_profit: Decimal | None
    client_id: str


class Day26Mt5ExecutionService:
    """Execute one accepted V1 signal on the owner's connected demo account."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        read_gateway: MetaApiReadGateway,
        margin_gateway: MetaApiMarginGateway,
        trade_gateway: MetaApiTradeGateway,
        zone_wait_seconds: float = 300.0,
        zone_poll_seconds: float = 2.0,
        entry_tolerance: Decimal | str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._read_gateway = read_gateway
        self._margin_gateway = margin_gateway
        self._trade_gateway = trade_gateway
        self._zone_wait_seconds = max(0.0, float(zone_wait_seconds))
        self._zone_poll_seconds = max(0.05, float(zone_poll_seconds))
        self._entry_tolerance = _resolve_entry_tolerance(entry_tolerance)

    async def execute_owner_demo_signal(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        risk_percent: Decimal | str | float,
        double_lot_approved: bool,
    ) -> Day26ExecutionResult:
        signal, account = self._load_inputs(owner_user_id, signal_id)
        token = self._decrypt_token(account)

        day23 = Day23Mt5ReadService(
            session_factory=self._session_factory,
            cipher=self._cipher,
            gateway=self._read_gateway,
        )
        try:
            live_state = await day23.read_owner_live_state(owner_user_id)
        except Day23ReadError as exc:
            raise Day26ExecutionError(exc.code) from exc

        execution_entry, live_state = await self._resolve_entry(
            owner_user_id=owner_user_id,
            signal=signal,
            day23=day23,
            initial_state=live_state,
        )

        try:
            specification = await self._read_gateway.read_symbol_specification(
                token=token,
                account_id=account.metaapi_account_id,
                region=live_state.region,
                symbol=signal.symbol,
            )
        except MetaApiGatewayError as exc:
            raise Day26ExecutionError(exc.code) from exc

        sizing = self._size_signal(
            signal=signal,
            execution_entry=execution_entry,
            balance=live_state.account.balance,
            price_loss_tick_value=live_state.price.loss_tick_value,
            specification=specification,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )

        # Day 25 remains unchanged. For an exact signal, sizing carries the provider's
        # exact price. For a zone, the deterministic zone gate has already authorized
        # the current executable broker price, so that actual in-zone price is passed
        # through the same one-shot price/funds preflight.
        preflight = Day25TradePreflightService(margin_gateway=self._margin_gateway)
        day25_result = await preflight.evaluate(
            live_state=live_state,
            side=signal.side,
            sizing=sizing,
            token=token,
        )
        if not day25_result.proceed:
            self._audit_blocked(
                owner_user_id=owner_user_id,
                signal_id=signal.signal_id,
                code=day25_result.block_reason or "day25_preflight_blocked",
            )
            raise Day26ExecutionError(
                day25_result.block_reason or "day25_preflight_blocked"
            )

        # Freeze-check immediately before any local position is created. An edit or
        # cancellation arriving during a zone wait must stop the trade.
        self._assert_signal_still_current(owner_user_id, signal)

        planned = self._create_planned_positions(
            owner_user_id=owner_user_id,
            signal=signal,
            sizing=sizing,
            execution_entry=execution_entry,
        )

        order_ids: dict[str, str] = {}
        try:
            for item in planned:
                result = await self._trade_gateway.place_market_order(
                    token=token,
                    account_id=account.metaapi_account_id,
                    region=live_state.region,
                    side=signal.side,
                    symbol=signal.symbol,
                    volume=float(sizing.volume),
                    stop_loss=float(signal.stop_loss),
                    take_profit=(
                        float(item.take_profit)
                        if item.take_profit is not None
                        else None
                    ),
                    client_id=item.client_id,
                )
                order_ids[item.client_id] = result.order_id
                self._record_order_id(item.local_position_id, result.order_id)
        except MetaApiGatewayError as exc:
            self._record_execution_failure(
                owner_user_id=owner_user_id,
                signal_id=signal.signal_id,
                code=exc.code,
                submitted_order_count=len(order_ids),
            )
            raise Day26ExecutionError(exc.code) from exc

        try:
            broker_positions = await self._read_gateway.read_positions(
                token=token,
                account_id=account.metaapi_account_id,
                region=live_state.region,
            )
        except MetaApiGatewayError as exc:
            self._record_execution_failure(
                owner_user_id=owner_user_id,
                signal_id=signal.signal_id,
                code=exc.code,
                submitted_order_count=len(order_ids),
            )
            raise Day26ExecutionError(exc.code) from exc

        mapped = self._map_broker_positions(
            owner_user_id=owner_user_id,
            signal=signal,
            sizing=sizing,
            execution_entry=execution_entry,
            planned=planned,
            order_ids=order_ids,
            broker_positions=broker_positions,
        )
        self._audit_success(
            owner_user_id=owner_user_id,
            signal=signal,
            sizing=sizing,
            execution_entry=execution_entry,
            mapped=mapped,
        )
        return Day26ExecutionResult(
            signal_id=signal.signal_id,
            user_id=owner_user_id,
            symbol=signal.symbol,
            side=signal.side,
            signal_entry_price=execution_entry,
            stop_loss=signal.stop_loss,
            base_risk_percent=sizing.base_risk_percent,
            effective_risk_percent=sizing.effective_risk_percent,
            double_lot_applied=sizing.double_lot_applied,
            positions=mapped,
        )

    async def _resolve_entry(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        day23: Day23Mt5ReadService,
        initial_state: Day23LiveState,
    ) -> tuple[Decimal, Day23LiveState]:
        if not signal.is_zone:
            # A provider posting a single round number ("SELL 4386") is stating where
            # they want in, not asserting that the tick will print at exactly that
            # cent. Requiring strict equality against the live bid/ask made almost
            # every single-price provider signal unexecutable, because gold moves in
            # cents and the price is sampled once with no chasing.
            #
            # Accept the current executable price when it sits within a bounded
            # tolerance of the stated entry, and return that live price so the trade
            # is sized off the actual fill. Risk therefore stays exactly the user's
            # configured percentage rather than drifting with the difference. This is
            # the same mechanism a zone already uses.
            #
            # Outside the tolerance nothing changes: the provider's price is returned
            # unchanged and the Day 25 gate still blocks with entry_price_unavailable.
            # There is no chase, no wait, no retry and no substitution of a "better"
            # price - the sample is taken once, exactly as before.
            if self._entry_tolerance <= 0:
                return signal.entry_low, initial_state
            try:
                executable = Decimal(
                    str(Day23Mt5ReadService.executable_price(initial_state, signal.side))
                )
            except Day23ReadError:
                # Let the unchanged Day 25 preflight report the price failure.
                return signal.entry_low, initial_state
            if abs(executable - signal.entry_low) <= self._entry_tolerance:
                return executable, initial_state
            return signal.entry_low, initial_state

        posted_at = signal.source_posted_at
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=UTC)
        deadline = posted_at.astimezone(UTC) + timedelta(seconds=self._zone_wait_seconds)

        state = initial_state
        while True:
            self._assert_signal_still_current(owner_user_id, signal)
            try:
                executable = Decimal(
                    str(Day23Mt5ReadService.executable_price(state, signal.side))
                )
            except Day23ReadError as exc:
                raise Day26ExecutionError(exc.code) from exc

            if signal.entry_low <= executable <= signal.entry_high:
                return executable, state

            now = datetime.now(UTC)
            if self._zone_wait_seconds <= 0 or now >= deadline:
                self._audit_blocked(
                    owner_user_id=owner_user_id,
                    signal_id=signal.signal_id,
                    code="zone_not_reached",
                )
                raise Day26ExecutionError("zone_not_reached")

            remaining = max(0.0, (deadline - now).total_seconds())
            await asyncio.sleep(min(self._zone_poll_seconds, remaining))
            try:
                state = await day23.read_owner_live_state(owner_user_id)
            except Day23ReadError as exc:
                raise Day26ExecutionError(exc.code) from exc

    def _load_inputs(
        self,
        owner_user_id: UUID,
        signal_id: UUID,
    ) -> tuple[_SignalInput, _AccountInput]:
        with self._session_factory() as session:
            signal_row = session.execute(
                text(
                    """
                    SELECT id, symbol, side, order_type, entry_low, entry_high,
                           stop_loss, take_profits, has_open_runner, parser_status,
                           risk_multiplier, source_revision_index, source_posted_at
                    FROM signals
                    WHERE id = :signal_id
                    FOR UPDATE
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if signal_row is None:
                raise Day26ExecutionError("signal_not_found")
            if str(signal_row["parser_status"]) != "accepted":
                raise Day26ExecutionError("signal_not_accepted")
            if str(signal_row["order_type"]) != "market":
                raise Day26ExecutionError("day26_market_signal_required")

            existing_count = session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE signal_id = :signal_id AND user_id = :user_id
                    """
                ),
                {"signal_id": signal_id, "user_id": owner_user_id},
            ).scalar_one()
            if int(existing_count) != 0:
                raise Day26ExecutionError("signal_execution_already_started")

            cancelled = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM signal_lifecycle_events
                            WHERE signal_id = :signal_id
                              AND event_type = 'cancel'
                        )
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalar_one()
            )
            if cancelled:
                raise Day26ExecutionError("signal_cancelled")

            account_row = session.execute(
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
            if account_row is None:
                raise Day26ExecutionError("mt5_account_not_configured")
            if str(account_row["account_environment"]).lower() != "demo":
                raise Day26ExecutionError("day26_demo_account_required")
            if str(account_row["status"]) != "connected":
                raise Day26ExecutionError("mt5_account_not_connected")

        symbol = str(signal_row["symbol"] or "").strip().upper()
        side = str(signal_row["side"] or "").strip().upper()
        if symbol != "XAUUSD":
            raise Day26ExecutionError("day26_xauusd_required")
        if side not in {"BUY", "SELL"}:
            raise Day26ExecutionError("trade_side_invalid")

        entry_low = self._required_decimal(
            signal_row["entry_low"], "signal_entry_invalid"
        )
        entry_high = self._required_decimal(
            signal_row["entry_high"], "signal_entry_invalid"
        )
        if entry_high < entry_low:
            raise Day26ExecutionError("signal_entry_invalid")
        stop_loss = self._required_decimal(
            signal_row["stop_loss"], "signal_stop_loss_invalid"
        )
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
        if not isinstance(source_posted_at, datetime):
            raise Day26ExecutionError("signal_posted_at_invalid")

        return (
            _SignalInput(
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
            ),
            _AccountInput(
                local_account_id=account_row["id"],
                metaapi_account_id=str(account_row["metaapi_account_id"]),
                token_ciphertext=bytes(account_row["metaapi_token_ciphertext"]),
            ),
        )

    def _assert_signal_still_current(
        self,
        owner_user_id: UUID,
        signal: _SignalInput,
    ) -> None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT parser_status, source_revision_index
                    FROM signals
                    WHERE id = :signal_id
                    """
                ),
                {"signal_id": signal.signal_id},
            ).mappings().first()
            if row is None:
                raise Day26ExecutionError("signal_not_found")
            if str(row["parser_status"]) != "accepted":
                raise Day26ExecutionError("signal_no_longer_accepted")
            if int(row["source_revision_index"]) != signal.source_revision_index:
                raise Day26ExecutionError("signal_changed_before_execution")

            existing = int(
                session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM positions
                        WHERE signal_id = :signal_id AND user_id = :user_id
                        """
                    ),
                    {"signal_id": signal.signal_id, "user_id": owner_user_id},
                ).scalar_one()
            )
            if existing:
                raise Day26ExecutionError("signal_execution_already_started")

            cancelled = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM signal_lifecycle_events
                            WHERE signal_id = :signal_id
                              AND event_type = 'cancel'
                        )
                        """
                    ),
                    {"signal_id": signal.signal_id},
                ).scalar_one()
            )
            if cancelled:
                raise Day26ExecutionError("signal_cancelled")

    def _decrypt_token(self, account: _AccountInput) -> str:
        try:
            token = self._cipher.decrypt(account.token_ciphertext).strip()
        except BrokerCredentialDecryptionError as exc:
            raise Day26ExecutionError("broker_credential_decryption_failed") from exc
        if len(token) < 20:
            raise Day26ExecutionError("metaapi_platform_token_not_configured")
        return token

    def _size_signal(
        self,
        *,
        signal: _SignalInput,
        execution_entry: Decimal,
        balance: float,
        price_loss_tick_value: float | None,
        specification: dict[str, object],
        risk_percent: Decimal | str | float,
        double_lot_approved: bool,
    ) -> Day24RiskSizingResult:
        if price_loss_tick_value is None:
            raise Day26ExecutionError("loss_tick_value_unavailable")
        try:
            rules = BrokerVolumeRules.from_values(
                minimum=specification.get("minVolume"),
                maximum=specification.get("maxVolume"),
                step=specification.get("volumeStep"),
            )
            return Day24RiskSizer.size(
                balance=balance,
                risk_percent=risk_percent,
                signal_entry_price=execution_entry,
                signal_stop_loss=signal.stop_loss,
                tick_size=specification.get("tickSize"),
                tick_value=price_loss_tick_value,
                take_profit_count=signal.position_count,
                volume_rules=rules,
                signal_requests_double_lot=signal.signal_requests_double_lot,
                double_lot_approved=double_lot_approved,
            )
        except Day24RiskSizingError as exc:
            raise Day26ExecutionError(exc.code) from exc

    def _create_planned_positions(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        sizing: Day24RiskSizingResult,
        execution_entry: Decimal,
    ) -> tuple[_PlannedPosition, ...]:
        targets: list[Decimal | None] = list(signal.take_profits)
        if signal.has_open_runner:
            targets.append(None)

        planned: list[_PlannedPosition] = []
        with self._session_factory() as session:
            for tp_index, take_profit in enumerate(targets, start=1):
                local_id = uuid4()
                client_id = f"SS_{local_id.hex[:12]}_{tp_index}"
                session.execute(
                    text(
                        """
                        INSERT INTO positions (
                            id, signal_id, user_id, tp_index, take_profit,
                            planned_risk_percent, volume, stop_loss,
                            broker_client_id, status, entry_price
                        ) VALUES (
                            :id, :signal_id, :user_id, :tp_index, :take_profit,
                            :risk_percent, :volume, :stop_loss,
                            :client_id, 'planned', :entry_price
                        )
                        """
                    ),
                    {
                        "id": local_id,
                        "signal_id": signal.signal_id,
                        "user_id": owner_user_id,
                        "tp_index": tp_index,
                        "take_profit": take_profit,
                        "risk_percent": sizing.effective_risk_percent,
                        "volume": sizing.volume,
                        "stop_loss": signal.stop_loss,
                        "client_id": client_id,
                        "entry_price": execution_entry,
                    },
                )
                planned.append(
                    _PlannedPosition(
                        local_position_id=local_id,
                        tp_index=tp_index,
                        take_profit=take_profit,
                        client_id=client_id,
                    )
                )
            session.commit()
        return tuple(planned)

    def _record_order_id(self, local_position_id: UUID, order_id: str) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_order_id = :order_id, updated_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": local_position_id, "order_id": order_id},
            )
            session.commit()

    def _map_broker_positions(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        sizing: Day24RiskSizingResult,
        execution_entry: Decimal,
        planned: tuple[_PlannedPosition, ...],
        order_ids: dict[str, str],
        broker_positions: list[dict[str, object]],
    ) -> tuple[Day26MappedPosition, ...]:
        by_client_id = {
            str(row.get("clientId")): row
            for row in broker_positions
            if row.get("clientId")
        }
        mapped: list[Day26MappedPosition] = []
        for item in planned:
            broker = by_client_id.get(item.client_id)
            if broker is None:
                raise Day26ExecutionError("broker_position_mapping_missing")
            self._validate_broker_position(
                broker=broker,
                signal=signal,
                take_profit=item.take_profit,
                volume=sizing.volume,
            )
            broker_position_id = str(broker.get("id") or "").strip()
            if not broker_position_id:
                raise Day26ExecutionError("broker_position_mapping_missing")
            broker_open_price = self._required_decimal(
                broker.get("openPrice"), "broker_position_mapping_invalid"
            )
            broker_order_id = order_ids.get(item.client_id, "")
            if not broker_order_id:
                raise Day26ExecutionError("broker_order_mapping_missing")

            with self._session_factory() as session:
                session.execute(
                    text(
                        """
                        UPDATE positions
                        SET broker_position_id = :broker_position_id,
                            broker_order_id = :broker_order_id,
                            entry_price = :entry_price,
                            status = 'open',
                            opened_at = :opened_at,
                            updated_at = :opened_at
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": item.local_position_id,
                        "broker_position_id": broker_position_id,
                        "broker_order_id": broker_order_id,
                        "entry_price": broker_open_price,
                        "opened_at": datetime.now(UTC),
                    },
                )
                session.commit()

            mapped.append(
                Day26MappedPosition(
                    local_position_id=item.local_position_id,
                    tp_index=item.tp_index,
                    take_profit=item.take_profit,
                    volume=sizing.volume,
                    client_id=item.client_id,
                    broker_order_id=broker_order_id,
                    broker_position_id=broker_position_id,
                    broker_open_price=broker_open_price,
                )
            )
        return tuple(mapped)

    def _validate_broker_position(
        self,
        *,
        broker: dict[str, object],
        signal: _SignalInput,
        take_profit: Decimal | None,
        volume: Decimal,
    ) -> None:
        raw_type = str(broker.get("type") or "")
        broker_side = (
            "BUY"
            if raw_type == "POSITION_TYPE_BUY"
            else "SELL"
            if raw_type == "POSITION_TYPE_SELL"
            else raw_type
        )
        if str(broker.get("symbol") or "").upper() != signal.symbol:
            raise Day26ExecutionError("broker_position_mapping_invalid")
        if broker_side != signal.side:
            raise Day26ExecutionError("broker_position_mapping_invalid")
        if self._required_decimal(
            broker.get("volume"), "broker_position_mapping_invalid"
        ) != volume:
            raise Day26ExecutionError("broker_position_mapping_invalid")
        if self._required_decimal(
            broker.get("stopLoss"), "broker_position_mapping_invalid"
        ) != signal.stop_loss:
            raise Day26ExecutionError("broker_position_mapping_invalid")

        broker_tp = self._optional_decimal(broker.get("takeProfit"))
        if take_profit is None:
            if broker_tp is not None and broker_tp != Decimal("0"):
                raise Day26ExecutionError("broker_position_mapping_invalid")
        elif broker_tp != take_profit:
            raise Day26ExecutionError("broker_position_mapping_invalid")

    def _audit_blocked(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        code: str,
    ) -> None:
        self._audit(
            owner_user_id=owner_user_id,
            signal_id=signal_id,
            event_type="mt5.day26_execution_blocked",
            payload={"error_code": code, "trade_action_created": False},
        )

    def _record_execution_failure(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        code: str,
        submitted_order_count: int,
    ) -> None:
        self._audit(
            owner_user_id=owner_user_id,
            signal_id=signal_id,
            event_type="mt5.day26_execution_failure",
            payload={
                "error_code": code,
                "submitted_order_count": submitted_order_count,
                "automatic_retry": False,
            },
        )

    def _audit_success(
        self,
        *,
        owner_user_id: UUID,
        signal: _SignalInput,
        sizing: Day24RiskSizingResult,
        execution_entry: Decimal,
        mapped: tuple[Day26MappedPosition, ...],
    ) -> None:
        self._audit(
            owner_user_id=owner_user_id,
            signal_id=signal.signal_id,
            event_type="mt5.day26_execution_success",
            payload={
                "symbol": signal.symbol,
                "side": signal.side,
                "provider_entry_low": str(signal.entry_low),
                "provider_entry_high": str(signal.entry_high),
                "execution_entry": str(execution_entry),
                "entry_is_zone": signal.is_zone,
                "position_count": len(mapped),
                "open_runner": signal.has_open_runner,
                "base_risk_percent": str(sizing.base_risk_percent),
                "effective_risk_percent": str(sizing.effective_risk_percent),
                "double_lot_applied": sizing.double_lot_applied,
                "all_positions_mapped": True,
                "automatic_retry": False,
            },
        )

    def _audit(
        self,
        *,
        owner_user_id: UUID,
        signal_id: UUID,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=owner_user_id,
                    event_type=event_type,
                    entity_type="signal",
                    entity_id=signal_id,
                    payload=payload,
                )
            )
            session.commit()

    @classmethod
    def _take_profits(cls, value: object) -> tuple[Decimal, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise Day26ExecutionError("signal_take_profits_invalid")
        return tuple(
            cls._required_decimal(item, "signal_take_profits_invalid")
            for item in value
        )

    @staticmethod
    def _directionally_valid(
        *,
        side: str,
        entry_low: Decimal,
        entry_high: Decimal,
        stop_loss: Decimal,
        take_profits: tuple[Decimal, ...],
    ) -> bool:
        if side == "BUY":
            return (
                stop_loss < entry_low
                and all(tp > entry_high for tp in take_profits)
                and all(
                    right > left
                    for left, right in zip(take_profits, take_profits[1:])
                )
            )
        return (
            stop_loss > entry_high
            and all(tp < entry_low for tp in take_profits)
            and all(
                right < left
                for left, right in zip(take_profits, take_profits[1:])
            )
        )

    @staticmethod
    def _optional_decimal(value: object) -> Decimal | None:
        if value in {None, ""}:
            return None
        if isinstance(value, bool):
            raise Day26ExecutionError("broker_position_mapping_invalid")
        try:
            result = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise Day26ExecutionError("broker_position_mapping_invalid") from exc
        if not result.is_finite():
            raise Day26ExecutionError("broker_position_mapping_invalid")
        return result

    @staticmethod
    def _required_decimal(value: object, code: str) -> Decimal:
        if isinstance(value, bool) or value is None:
            raise Day26ExecutionError(code)
        try:
            result = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise Day26ExecutionError(code) from exc
        if not result.is_finite() or result <= 0:
            raise Day26ExecutionError(code)
        return result
