"""Day 26 exact provider-following multi-TP demo execution.

The input is an already-canonical Signal produced by Days 15-18. This module
reuses Day 23 broker state, Day 24 risk sizing and Day 25 price/funds gates,
then submits one market order per provider TP to the owner's Vantage demo.

It does not classify, parse, reinterpret, chase, modify, close or retry trades.
Day 27 owns follow-up trade management and Day 28 owns automatic end-to-end
Telegram-to-broker execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError
from app.risk_sizing_day24 import (
    BrokerVolumeRules,
    Day24RiskSizer,
    Day24RiskSizingError,
    Day24RiskSizingResult,
)
from app.trade_preflight_day25 import Day25TradePreflightService


class Day26ExecutionError(RuntimeError):
    """Sanitized Day 26 execution failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Day26MappedPosition:
    local_position_id: UUID
    tp_index: int
    take_profit: Decimal
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
    entry_price: Decimal
    stop_loss: Decimal
    take_profits: tuple[Decimal, ...]
    signal_requests_double_lot: bool


@dataclass(frozen=True, slots=True)
class _AccountInput:
    local_account_id: UUID
    metaapi_account_id: str
    token_ciphertext: bytes


@dataclass(frozen=True, slots=True)
class _PlannedPosition:
    local_position_id: UUID
    tp_index: int
    take_profit: Decimal
    client_id: str


class Day26Mt5ExecutionService:
    """Execute one canonical signal on the owner's connected demo account."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        read_gateway: MetaApiReadGateway,
        margin_gateway: MetaApiMarginGateway,
        trade_gateway: MetaApiTradeGateway,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._read_gateway = read_gateway
        self._margin_gateway = margin_gateway
        self._trade_gateway = trade_gateway

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
            balance=live_state.account.balance,
            price_loss_tick_value=live_state.price.loss_tick_value,
            specification=specification,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )

        preflight = Day25TradePreflightService(
            margin_gateway=self._margin_gateway
        )
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

        planned = self._create_planned_positions(
            owner_user_id=owner_user_id,
            signal=signal,
            sizing=sizing,
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
                    take_profit=float(item.take_profit),
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
            planned=planned,
            order_ids=order_ids,
            broker_positions=broker_positions,
        )
        self._audit_success(
            owner_user_id=owner_user_id,
            signal=signal,
            sizing=sizing,
            mapped=mapped,
        )
        return Day26ExecutionResult(
            signal_id=signal.signal_id,
            user_id=owner_user_id,
            symbol=signal.symbol,
            side=signal.side,
            signal_entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            base_risk_percent=sizing.base_risk_percent,
            effective_risk_percent=sizing.effective_risk_percent,
            double_lot_applied=sizing.double_lot_applied,
            positions=mapped,
        )

    def _load_inputs(
        self, owner_user_id: UUID, signal_id: UUID
    ) -> tuple[_SignalInput, _AccountInput]:
        with self._session_factory() as session:
            signal_row = session.execute(
                text(
                    """
                    SELECT id, symbol, side, order_type, entry_low, entry_high,
                           stop_loss, take_profits, parser_status, risk_multiplier
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

        entry_low = self._required_decimal(signal_row["entry_low"], "signal_entry_invalid")
        entry_high = self._required_decimal(signal_row["entry_high"], "signal_entry_invalid")
        if entry_low != entry_high:
            raise Day26ExecutionError("day26_exact_entry_required")
        stop_loss = self._required_decimal(
            signal_row["stop_loss"], "signal_stop_loss_invalid"
        )
        take_profits = self._take_profits(signal_row["take_profits"])
        risk_multiplier = self._required_decimal(
            signal_row["risk_multiplier"], "signal_risk_multiplier_invalid"
        )

        return (
            _SignalInput(
                signal_id=signal_id,
                symbol=symbol,
                side=side,
                entry_price=entry_low,
                stop_loss=stop_loss,
                take_profits=take_profits,
                signal_requests_double_lot=risk_multiplier > Decimal("1"),
            ),
            _AccountInput(
                local_account_id=account_row["id"],
                metaapi_account_id=str(account_row["metaapi_account_id"]),
                token_ciphertext=bytes(account_row["metaapi_token_ciphertext"]),
            ),
        )

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
                signal_entry_price=signal.entry_price,
                signal_stop_loss=signal.stop_loss,
                tick_size=specification.get("tickSize"),
                tick_value=price_loss_tick_value,
                take_profit_count=len(signal.take_profits),
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
    ) -> tuple[_PlannedPosition, ...]:
        planned: list[_PlannedPosition] = []
        with self._session_factory() as session:
            for tp_index, take_profit in enumerate(signal.take_profits, start=1):
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
                        "entry_price": signal.entry_price,
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
        take_profit: Decimal,
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
        if self._required_decimal(
            broker.get("takeProfit"), "broker_position_mapping_invalid"
        ) != take_profit:
            raise Day26ExecutionError("broker_position_mapping_invalid")

    def _audit_blocked(
        self, *, owner_user_id: UUID, signal_id: UUID, code: str
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
        mapped: tuple[Day26MappedPosition, ...],
    ) -> None:
        self._audit(
            owner_user_id=owner_user_id,
            signal_id=signal.signal_id,
            event_type="mt5.day26_execution_success",
            payload={
                "symbol": signal.symbol,
                "side": signal.side,
                "position_count": len(mapped),
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
            cls._required_decimal(item, "signal_take_profits_invalid") for item in value
        )

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
