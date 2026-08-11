"""Day 23 read-only Vantage MT5 account and XAUUSD state service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_token_scope import inspect_metaapi_token_scope
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

DAY23_SYMBOL = "XAUUSD"
DEFAULT_MAX_QUOTE_AGE_SECONDS = 15.0


class Day23ReadError(RuntimeError):
    """Sanitized read-state error safe to return to the owner UI."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Day23AccountState:
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float | None
    leverage: float | None
    trade_allowed: bool


@dataclass(frozen=True, slots=True)
class Day23Position:
    position_id: str
    symbol: str
    side: str
    volume: float
    open_price: float
    current_price: float | None
    stop_loss: float | None
    take_profit: float | None
    profit: float | None
    swap: float | None
    commission: float | None
    opened_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class Day23PriceState:
    symbol: str
    bid: float | None
    ask: float | None
    buy_price: float | None
    sell_price: float | None
    quote_time: datetime | None
    quote_age_seconds: float | None
    available: bool
    stale: bool
    execution_ready: bool
    block_reason: str | None


@dataclass(frozen=True, slots=True)
class Day23LiveState:
    local_account_id: UUID
    metaapi_account_id: str
    login_masked: str
    server: str
    region: str
    read_at: datetime
    account: Day23AccountState
    price: Day23PriceState
    positions: tuple[Day23Position, ...]
    execution_ready: bool
    execution_block_reason: str | None


class Day23Mt5ReadService:
    """Read live terminal state without exposing any trading capability."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        gateway: MetaApiReadGateway,
        max_quote_age_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway
        configured_age = max_quote_age_seconds
        if configured_age is None:
            raw = os.getenv("SUPER_SIGNALS_DAY23_MAX_QUOTE_AGE_SECONDS", "").strip()
            try:
                configured_age = float(raw) if raw else DEFAULT_MAX_QUOTE_AGE_SECONDS
            except ValueError:
                configured_age = DEFAULT_MAX_QUOTE_AGE_SECONDS
        self._max_quote_age_seconds = min(max(float(configured_age), 1.0), 60.0)

    async def read_owner_live_state(
        self,
        owner_user_id: UUID,
        *,
        now: datetime | None = None,
    ) -> Day23LiveState:
        row = self._load_row(owner_user_id)
        if row is None:
            raise Day23ReadError("mt5_account_not_configured")
        if str(row["status"]) != "connected":
            raise Day23ReadError("mt5_account_not_connected", retryable=True)

        try:
            token = self._cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
        except BrokerCredentialDecryptionError as exc:
            self._audit_failure(row["id"], "broker_credential_decryption_failed", "decrypt")
            raise Day23ReadError("broker_credential_decryption_failed") from exc

        scope = inspect_metaapi_token_scope(token)
        if (
            scope.jwt_payload_decoded
            and scope.is_explicitly_narrowed
            and not scope.has_terminal_access
        ):
            self._audit_failure(row["id"], "metaapi_terminal_scope_missing", "token_scope")
            raise Day23ReadError("metaapi_terminal_scope_missing")

        account_id = str(row["metaapi_account_id"])
        try:
            region = await self._gateway.resolve_account_region(
                token=token,
                account_id=account_id,
            )
        except MetaApiGatewayError as exc:
            self._audit_failure(row["id"], exc.code, "resolve_region")
            raise Day23ReadError(exc.code, retryable=exc.retryable) from exc

        try:
            account_payload = await self._gateway.read_account_information(
                token=token, account_id=account_id, region=region
            )
        except MetaApiGatewayError as exc:
            self._audit_failure(row["id"], exc.code, "account_information", region=region)
            raise Day23ReadError(exc.code, retryable=exc.retryable) from exc

        try:
            positions_payload = await self._gateway.read_positions(
                token=token, account_id=account_id, region=region
            )
        except MetaApiGatewayError as exc:
            self._audit_failure(row["id"], exc.code, "positions", region=region)
            raise Day23ReadError(exc.code, retryable=exc.retryable) from exc

        try:
            price_payload = await self._gateway.read_symbol_price(
                token=token,
                account_id=account_id,
                region=region,
                symbol=DAY23_SYMBOL,
            )
        except MetaApiGatewayError as exc:
            self._audit_failure(row["id"], exc.code, "symbol_price", region=region)
            raise Day23ReadError(exc.code, retryable=exc.retryable) from exc

        read_at = self._utc(now or datetime.now(UTC))
        try:
            account = self._account_state(account_payload)
            positions = tuple(self._position(item) for item in positions_payload)
            price = self._price_state(price_payload, read_at=read_at)
        except Day23ReadError as exc:
            self._audit_failure(row["id"], exc.code, "parse_response", region=region)
            raise

        state = Day23LiveState(
            local_account_id=row["id"],
            metaapi_account_id=account_id,
            login_masked=self._mask_login(str(row["login"])),
            server=str(row["server"]),
            region=region,
            read_at=read_at,
            account=account,
            price=price,
            positions=positions,
            execution_ready=price.execution_ready,
            execution_block_reason=price.block_reason,
        )
        self._audit_success(state)
        return state

    @staticmethod
    def executable_price(state: Day23LiveState, side: str) -> float:
        """Return the broker-executable side price, refusing stale/unavailable data."""
        if not state.execution_ready:
            raise Day23ReadError(state.execution_block_reason or "price_unavailable")
        normalized = side.strip().upper()
        if normalized == "BUY" and state.price.ask is not None:
            return state.price.ask
        if normalized == "SELL" and state.price.bid is not None:
            return state.price.bid
        raise Day23ReadError("trade_side_invalid")

    def _price_state(
        self,
        payload: dict[str, object],
        *,
        read_at: datetime,
    ) -> Day23PriceState:
        bid = self._optional_positive_float(payload.get("bid"))
        ask = self._optional_positive_float(payload.get("ask"))
        quote_time = self._parse_datetime(payload.get("time"))
        available = bid is not None and ask is not None and quote_time is not None
        age: float | None = None
        stale = False
        block_reason: str | None = None
        if not available:
            block_reason = "price_unavailable"
        else:
            age = max(0.0, (read_at - quote_time).total_seconds())
            stale = age > self._max_quote_age_seconds
            if stale:
                block_reason = "price_stale"
        ready = available and not stale
        return Day23PriceState(
            symbol=str(payload.get("symbol") or DAY23_SYMBOL),
            bid=bid,
            ask=ask,
            buy_price=ask,
            sell_price=bid,
            quote_time=quote_time,
            quote_age_seconds=age,
            available=available,
            stale=stale,
            execution_ready=ready,
            block_reason=block_reason,
        )

    @classmethod
    def _account_state(cls, payload: dict[str, object]) -> Day23AccountState:
        return Day23AccountState(
            currency=str(payload.get("currency") or ""),
            balance=cls._required_float(payload.get("balance")),
            equity=cls._required_float(payload.get("equity")),
            margin=cls._required_float(payload.get("margin")),
            free_margin=cls._required_float(payload.get("freeMargin")),
            margin_level=cls._optional_float(payload.get("marginLevel")),
            leverage=cls._optional_float(payload.get("leverage")),
            trade_allowed=bool(payload.get("tradeAllowed", False)),
        )

    @classmethod
    def _position(cls, payload: dict[str, object]) -> Day23Position:
        raw_type = str(payload.get("type") or "")
        side = (
            "BUY"
            if raw_type == "POSITION_TYPE_BUY"
            else "SELL"
            if raw_type == "POSITION_TYPE_SELL"
            else raw_type
        )
        return Day23Position(
            position_id=str(payload.get("id") or ""),
            symbol=str(payload.get("symbol") or ""),
            side=side,
            volume=cls._required_float(payload.get("volume")),
            open_price=cls._required_float(payload.get("openPrice")),
            current_price=cls._optional_float(payload.get("currentPrice")),
            stop_loss=cls._optional_float(payload.get("stopLoss")),
            take_profit=cls._optional_float(payload.get("takeProfit")),
            profit=cls._optional_float(payload.get("profit")),
            swap=cls._optional_float(payload.get("swap")),
            commission=cls._optional_float(payload.get("commission")),
            opened_at=cls._parse_datetime(payload.get("time")),
            updated_at=cls._parse_datetime(payload.get("updateTime")),
        )

    def _load_row(self, owner_user_id: UUID) -> Any | None:
        with self._session_factory() as session:
            return session.execute(
                text(
                    """
                    SELECT *
                    FROM mt5_accounts
                    WHERE owner_user_id = :owner_user_id
                      AND status != 'revoked'
                    LIMIT 1
                    """
                ),
                {"owner_user_id": owner_user_id},
            ).mappings().first()

    def _audit_success(self, state: Day23LiveState) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.day23_live_state_read",
                    entity_type="mt5_account",
                    entity_id=state.local_account_id,
                    payload={
                        "symbol": state.price.symbol,
                        "region": state.region,
                        "currency": state.account.currency,
                        "balance": state.account.balance,
                        "equity": state.account.equity,
                        "margin": state.account.margin,
                        "free_margin": state.account.free_margin,
                        "trade_allowed": state.account.trade_allowed,
                        "bid": state.price.bid,
                        "ask": state.price.ask,
                        "quote_time": (
                            state.price.quote_time.isoformat()
                            if state.price.quote_time is not None
                            else None
                        ),
                        "quote_age_seconds": state.price.quote_age_seconds,
                        "price_available": state.price.available,
                        "price_stale": state.price.stale,
                        "execution_ready": state.execution_ready,
                        "execution_block_reason": state.execution_block_reason,
                        "position_count": len(state.positions),
                        "buy_price_source": "ask",
                        "sell_price_source": "bid",
                        "terminal_read_requests": 3,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _audit_failure(
        self,
        local_account_id: UUID,
        code: str,
        stage: str,
        *,
        region: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_type="mt5.day23_live_state_failure",
                    entity_type="mt5_account",
                    entity_id=local_account_id,
                    payload={
                        "stage": stage,
                        "region": region,
                        "error_code": code,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    @staticmethod
    def _required_float(value: object) -> float:
        if isinstance(value, bool):
            raise Day23ReadError("metaapi_invalid_response")
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise Day23ReadError("metaapi_invalid_response") from exc

    @classmethod
    def _optional_float(cls, value: object) -> float | None:
        if value is None:
            return None
        return cls._required_float(value)

    @classmethod
    def _optional_positive_float(cls, value: object) -> float | None:
        parsed = cls._optional_float(value)
        return parsed if parsed is not None and parsed > 0 else None

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return Day23Mt5ReadService._utc(parsed)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _mask_login(login: str) -> str:
        if len(login) <= 4:
            return "*" * len(login)
        return f"{'*' * max(4, len(login) - 4)}{login[-4:]}"
