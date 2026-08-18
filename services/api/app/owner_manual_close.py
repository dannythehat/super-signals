"""Owner-only manual market close for the paper/demo account.

This is an explicit human control from the Super Signals UI. It closes only mapped
Super Signals positions by their immutable broker position IDs. It never closes by
symbol, never opens/reopens anything, and refuses live-money MT5 accounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

_EXIT_ENTRY_TYPES = {"DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY"}


class OwnerManualCloseError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class OwnerManualCloseResult:
    requested_count: int
    closed_count: int
    already_closed_count: int
    failed_count: int
    closed_position_ids: tuple[UUID, ...]
    failed_position_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class _Account:
    account_id: str
    account_environment: str
    token_ciphertext: bytes


@dataclass(frozen=True, slots=True)
class _Position:
    id: UUID
    signal_id: UUID
    broker_position_id: str


def _decimal(value: object | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _broker_time(value: object | None, fallback: datetime) -> datetime:
    if not isinstance(value, str) or not value.strip():
        return fallback
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class OwnerManualCloseService:
    """Close one, one signal, or all mapped positions on the Owner demo account."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        read_gateway: MetaApiReadGateway,
        trade_gateway: MetaApiTradeGateway,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._read_gateway = read_gateway
        self._trade_gateway = trade_gateway

    async def close_position(self, user_id: UUID, position_id: UUID) -> OwnerManualCloseResult:
        positions = self._positions(user_id=user_id, position_id=position_id)
        if not positions:
            raise OwnerManualCloseError("owner_manual_position_not_open")
        return await self._close(user_id=user_id, positions=positions, scope="position")

    async def close_trade(self, user_id: UUID, signal_id: UUID) -> OwnerManualCloseResult:
        positions = self._positions(user_id=user_id, signal_id=signal_id)
        if not positions:
            raise OwnerManualCloseError("owner_manual_trade_not_open")
        return await self._close(user_id=user_id, positions=positions, scope="trade")

    async def close_all(self, user_id: UUID) -> OwnerManualCloseResult:
        positions = self._all_positions(user_id=user_id)
        if not positions:
            raise OwnerManualCloseError("owner_manual_all_not_open")
        return await self._close(user_id=user_id, positions=positions, scope="all")

    def _account(self, user_id: UUID) -> _Account:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id, account_environment,
                           metaapi_token_ciphertext, status
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status!='revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None or str(row["status"]) != "connected":
            raise OwnerManualCloseError("owner_manual_mt5_not_connected")
        environment = str(row["account_environment"] or "").strip().lower()
        if environment != "demo":
            raise OwnerManualCloseError("owner_manual_close_demo_only")
        return _Account(
            account_id=str(row["metaapi_account_id"]),
            account_environment=environment,
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )

    def _positions(
        self,
        *,
        user_id: UUID,
        position_id: UUID | None = None,
        signal_id: UUID | None = None,
    ) -> tuple[_Position, ...]:
        if (position_id is None) == (signal_id is None):
            raise ValueError("owner_manual_close_target_invalid")
        target_sql = "p.id=:target_id" if position_id is not None else "p.signal_id=:target_id"
        target_id = position_id if position_id is not None else signal_id
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    f"""
                    SELECT p.id, p.signal_id, p.broker_position_id
                    FROM positions AS p
                    WHERE p.user_id=:user_id
                      AND {target_sql}
                      AND p.status='open'
                      AND p.broker_position_id IS NOT NULL
                    ORDER BY p.created_at, p.tp_index, p.id
                    """
                ),
                {"user_id": user_id, "target_id": target_id},
            ).mappings().all()
        return tuple(
            _Position(
                id=row["id"],
                signal_id=row["signal_id"],
                broker_position_id=str(row["broker_position_id"]),
            )
            for row in rows
        )

    def _all_positions(self, *, user_id: UUID) -> tuple[_Position, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT p.id, p.signal_id, p.broker_position_id
                    FROM positions AS p
                    WHERE p.user_id=:user_id
                      AND p.status='open'
                      AND p.broker_position_id IS NOT NULL
                    ORDER BY p.created_at, p.signal_id, p.tp_index, p.id
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        return tuple(
            _Position(
                id=row["id"],
                signal_id=row["signal_id"],
                broker_position_id=str(row["broker_position_id"]),
            )
            for row in rows
        )

    async def _close(
        self,
        *,
        user_id: UUID,
        positions: tuple[_Position, ...],
        scope: str,
    ) -> OwnerManualCloseResult:
        account = self._account(user_id)
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
        except BrokerCredentialDecryptionError as exc:
            raise OwnerManualCloseError("broker_credential_decryption_failed") from exc

        try:
            region = await self._read_gateway.resolve_account_region(
                token=token,
                account_id=account.account_id,
            )
            broker_positions = await self._read_gateway.read_positions(
                token=token,
                account_id=account.account_id,
                region=region,
            )
        except MetaApiGatewayError as exc:
            raise OwnerManualCloseError(exc.code, retryable=exc.retryable) from exc

        open_broker_ids = {
            str(item.get("id") or "").strip()
            for item in broker_positions
            if str(item.get("id") or "").strip()
        }
        closed: list[UUID] = []
        already_closed: list[UUID] = []
        failed: list[UUID] = []

        for position in positions:
            if position.broker_position_id not in open_broker_ids:
                already_closed.append(position.id)
                continue
            try:
                await self._trade_gateway.close_position(
                    token=token,
                    account_id=account.account_id,
                    region=region,
                    position_id=position.broker_position_id,
                )
            except MetaApiGatewayError:
                failed.append(position.id)
                continue

            now = datetime.now(UTC)
            exit_price: Decimal | None = None
            occurred_at = now
            try:
                deals = await self._read_gateway.read_deals_by_position(
                    token=token,
                    account_id=account.account_id,
                    region=region,
                    position_id=position.broker_position_id,
                )
                exits = [
                    item
                    for item in deals
                    if str(item.get("entryType") or "").strip().upper() in _EXIT_ENTRY_TYPES
                ]
                if exits:
                    exits.sort(key=lambda item: _broker_time(item.get("time"), now))
                    latest = exits[-1]
                    exit_price = _decimal(latest.get("price"))
                    occurred_at = _broker_time(latest.get("time"), now)
            except MetaApiGatewayError:
                # The close itself already succeeded. History can reconcile the exact
                # exit price later without undoing or falsely retrying the broker close.
                pass

            self._record_close(
                user_id=user_id,
                position=position,
                scope=scope,
                exit_price=exit_price,
                occurred_at=occurred_at,
            )
            closed.append(position.id)

        if failed and not closed and not already_closed:
            raise OwnerManualCloseError("owner_manual_close_broker_failed", retryable=True)

        return OwnerManualCloseResult(
            requested_count=len(positions),
            closed_count=len(closed),
            already_closed_count=len(already_closed),
            failed_count=len(failed),
            closed_position_ids=tuple(closed),
            failed_position_ids=tuple(failed),
        )

    def _record_close(
        self,
        *,
        user_id: UUID,
        position: _Position,
        scope: str,
        exit_price: Decimal | None,
        occurred_at: datetime,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            updated = session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='closed',
                        closed_at=COALESCE(closed_at,:occurred_at),
                        exit_price=COALESCE(exit_price,:exit_price),
                        close_reason='owner_manual_close',
                        updated_at=:now
                    WHERE id=:position_id
                      AND user_id=:user_id
                      AND status='open'
                    RETURNING id
                    """
                ),
                {
                    "position_id": position.id,
                    "user_id": user_id,
                    "occurred_at": occurred_at,
                    "exit_price": exit_price,
                    "now": now,
                },
            ).scalar_one_or_none()
            if updated is None:
                session.rollback()
                return
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.owner_manual_close",
                    entity_type="position",
                    entity_id=position.id,
                    payload={
                        "action_type": "close_at_market",
                        "scope": scope,
                        "signal_id": str(position.signal_id),
                        "broker_position_id": position.broker_position_id,
                        "account_environment": "demo",
                        "exit_price": str(exit_price) if exit_price is not None else None,
                        "occurred_at": occurred_at.isoformat(),
                        "initiated_from": "super_signals_owner_ui",
                        "provider_instruction": False,
                    },
                )
            )
            session.commit()


__all__ = [
    "OwnerManualCloseError",
    "OwnerManualCloseResult",
    "OwnerManualCloseService",
]
