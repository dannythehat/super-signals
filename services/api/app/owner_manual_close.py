"""Owner manual market close for connected Smart Signals MT5 accounts.

This is an explicit human control from the Smart Signals UI. It closes only mapped
Smart Signals positions by their immutable broker position IDs. It never closes by
symbol and never opens/reopens anything. Live and demo accounts use the same path.

Manual close is deliberately retry-safe. PostgreSQL advisory locks serialize close
requests for the same mapped position, local state is re-read after the lock is acquired,
and ambiguous broker timeouts are reconciled against current broker state before the
request is reported as failed. This lets the UI retry a request lost behind a transient
Render 502 without sending two independent closes for the same mapped position.
"""

from __future__ import annotations

import asyncio
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
_AMBIGUOUS_CLOSE_RECHECK_DELAYS = (0.25, 0.75, 1.5)
_ALLOWED_ACCOUNT_ENVIRONMENTS = {"demo", "live"}


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
    """Close one position, one signal, or all mapped positions on a connected MT5 account."""

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

    async def close_position(
        self,
        user_id: UUID,
        position_id: UUID,
        *,
        scope: str = "position",
    ) -> OwnerManualCloseResult:
        positions = self._positions(user_id=user_id, position_id=position_id)
        if not positions:
            raise OwnerManualCloseError("owner_manual_position_not_open")
        return await self._close(user_id=user_id, positions=positions, scope=scope)

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
        if environment not in _ALLOWED_ACCOUNT_ENVIRONMENTS:
            raise OwnerManualCloseError("owner_manual_account_environment_invalid")
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

    @staticmethod
    def _acquire_position_locks(session: Session, positions: tuple[_Position, ...]) -> None:
        """Serialize manual-close attempts for the same mapped broker positions."""
        bind = session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        for position in sorted(positions, key=lambda item: str(item.id)):
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": f"owner_manual_close:{position.id}"},
            )

    @staticmethod
    def _open_after_lock(
        session: Session,
        *,
        user_id: UUID,
        positions: tuple[_Position, ...],
    ) -> tuple[tuple[_Position, ...], tuple[UUID, ...]]:
        open_positions: list[_Position] = []
        already_closed: list[UUID] = []
        for requested in positions:
            row = session.execute(
                text(
                    """
                    SELECT id, signal_id, broker_position_id, status
                    FROM positions
                    WHERE id=:position_id AND user_id=:user_id
                    LIMIT 1
                    """
                ),
                {"position_id": requested.id, "user_id": user_id},
            ).mappings().first()
            if (
                row is None
                or str(row["status"] or "").lower() != "open"
                or not str(row["broker_position_id"] or "").strip()
            ):
                already_closed.append(requested.id)
                continue
            open_positions.append(
                _Position(
                    id=row["id"],
                    signal_id=row["signal_id"],
                    broker_position_id=str(row["broker_position_id"]),
                )
            )
        return tuple(open_positions), tuple(already_closed)

    async def _broker_position_is_absent(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        broker_position_id: str,
    ) -> bool:
        """Resolve an ambiguous broker timeout before allowing the caller to retry."""
        for delay in _AMBIGUOUS_CLOSE_RECHECK_DELAYS:
            await asyncio.sleep(delay)
            try:
                broker_positions = await self._read_gateway.read_positions(
                    token=token,
                    account_id=account_id,
                    region=region,
                )
            except MetaApiGatewayError:
                continue
            open_ids = {
                str(item.get("id") or "").strip()
                for item in broker_positions
                if str(item.get("id") or "").strip()
            }
            return broker_position_id not in open_ids
        return False

    async def _exit_details(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        broker_position_id: str,
    ) -> tuple[Decimal | None, datetime]:
        now = datetime.now(UTC)
        try:
            deals = await self._read_gateway.read_deals_by_position(
                token=token,
                account_id=account_id,
                region=region,
                position_id=broker_position_id,
            )
            exits = [
                item
                for item in deals
                if str(item.get("entryType") or "").strip().upper() in _EXIT_ENTRY_TYPES
            ]
            if exits:
                exits.sort(key=lambda item: _broker_time(item.get("time"), now))
                latest = exits[-1]
                return _decimal(latest.get("price")), _broker_time(latest.get("time"), now)
        except MetaApiGatewayError:
            pass
        return None, now

    async def _close(
        self,
        *,
        user_id: UUID,
        positions: tuple[_Position, ...],
        scope: str,
    ) -> OwnerManualCloseResult:
        requested_count = len(positions)
        account = self._account(user_id)
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
        except BrokerCredentialDecryptionError as exc:
            raise OwnerManualCloseError("broker_credential_decryption_failed") from exc

        # Keep a transaction-scoped advisory lock for the full broker mutation. If a
        # 502 makes the browser retry, the second request waits, then re-reads local
        # state rather than issuing another close against the same broker position.
        lock_session = self._session_factory()
        try:
            self._acquire_position_locks(lock_session, positions)
            active_positions, already_local = self._open_after_lock(
                lock_session,
                user_id=user_id,
                positions=positions,
            )
            if not active_positions:
                return OwnerManualCloseResult(
                    requested_count=requested_count,
                    closed_count=0,
                    already_closed_count=len(already_local),
                    failed_count=0,
                    closed_position_ids=(),
                    failed_position_ids=(),
                )

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
            already_closed: list[UUID] = list(already_local)
            failed: list[UUID] = []

            for position in active_positions:
                if position.broker_position_id not in open_broker_ids:
                    exit_price, occurred_at = await self._exit_details(
                        token=token,
                        account_id=account.account_id,
                        region=region,
                        broker_position_id=position.broker_position_id,
                    )
                    self._record_close(
                        user_id=user_id,
                        position=position,
                        scope=f"{scope}_reconciled",
                        account_environment=account.account_environment,
                        exit_price=exit_price,
                        occurred_at=occurred_at,
                    )
                    already_closed.append(position.id)
                    continue

                try:
                    await self._trade_gateway.close_position(
                        token=token,
                        account_id=account.account_id,
                        region=region,
                        position_id=position.broker_position_id,
                    )
                except MetaApiGatewayError as exc:
                    # A timeout can mean the broker accepted the close but the HTTP
                    # response was lost. Re-read broker state before declaring failure.
                    confirmed_absent = False
                    if exc.retryable:
                        confirmed_absent = await self._broker_position_is_absent(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            broker_position_id=position.broker_position_id,
                        )
                    if not confirmed_absent:
                        failed.append(position.id)
                        continue

                exit_price, occurred_at = await self._exit_details(
                    token=token,
                    account_id=account.account_id,
                    region=region,
                    broker_position_id=position.broker_position_id,
                )
                self._record_close(
                    user_id=user_id,
                    position=position,
                    scope=scope,
                    account_environment=account.account_environment,
                    exit_price=exit_price,
                    occurred_at=occurred_at,
                )
                closed.append(position.id)

            if failed and not closed and not already_closed:
                raise OwnerManualCloseError("owner_manual_close_broker_failed", retryable=True)

            return OwnerManualCloseResult(
                requested_count=requested_count,
                closed_count=len(closed),
                already_closed_count=len(already_closed),
                failed_count=len(failed),
                closed_position_ids=tuple(closed),
                failed_position_ids=tuple(failed),
            )
        finally:
            # Rollback releases PostgreSQL transaction-scoped advisory locks. The
            # position updates themselves are committed by _record_close separately.
            lock_session.rollback()
            lock_session.close()

    def _record_close(
        self,
        *,
        user_id: UUID,
        position: _Position,
        scope: str,
        account_environment: str,
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
                        close_reason=:close_reason,
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
                    "close_reason": (
                        "stale_position_watchdog"
                        if scope == "stale_watchdog"
                        else "owner_manual_close"
                    ),
                },
            ).scalar_one_or_none()
            if updated is None:
                session.rollback()
                return
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type=(
                        "mt5.stale_position_watchdog_close"
                        if scope == "stale_watchdog"
                        else "mt5.owner_manual_close"
                    ),
                    entity_type="position",
                    entity_id=position.id,
                    payload={
                        "action_type": "close_at_market",
                        "scope": scope,
                        "signal_id": str(position.signal_id),
                        "broker_position_id": position.broker_position_id,
                        "account_environment": account_environment,
                        "exit_price": str(exit_price) if exit_price is not None else None,
                        "occurred_at": occurred_at.isoformat(),
                        "initiated_from": (
                            "stale_position_watchdog"
                            if scope == "stale_watchdog"
                            else "smart_signals_owner_ui"
                        ),
                        "provider_instruction": False,
                        "retry_safe": True,
                    },
                )
            )
            session.commit()


__all__ = [
    "OwnerManualCloseError",
    "OwnerManualCloseResult",
    "OwnerManualCloseService",
]
