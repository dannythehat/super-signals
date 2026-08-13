"""Day 36 broker-authoritative reconciliation of MT5 actions made outside the app.

This layer is deliberately read-only at the broker. It observes mapped Super Signals
positions, mirrors externally changed SL/TP values into local state, and records
manual terminal closes only when MT5 deal-reason evidence proves the close came from
Desktop, Mobile or Web. It never reopens, reverses, closes or modifies a broker trade.
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
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher
from app.trade_identity import public_trade_identity

MANUAL_ACTION_LABEL = "Manual action outside the app"
_MANUAL_DEAL_REASONS = {
    "DEAL_REASON_CLIENT": "MT5 desktop",
    "DEAL_REASON_MOBILE": "MT5 mobile",
    "DEAL_REASON_WEB": "MT5 web",
}
_EXIT_ENTRY_TYPES = {"DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY"}


class Day36ReconciliationError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Day36ManualAction:
    audit_id: UUID
    position_id: UUID
    action_type: str
    label: str
    detail: str
    old_value: str | None
    new_value: str | None
    trade_reference: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class Day36ReconciliationResult:
    user_id: UUID
    stop_loss_changes: int
    take_profit_changes: int
    manual_closes: int
    actions: tuple[Day36ManualAction, ...]
    broker_trade_action_created: bool = False


@dataclass(frozen=True, slots=True)
class _Account:
    account_id: str
    token_ciphertext: bytes


@dataclass(frozen=True, slots=True)
class _MappedPosition:
    id: UUID
    signal_id: UUID
    tp_index: int
    broker_position_id: str
    status: str
    stop_loss: Decimal | None
    take_profit: Decimal | None


def manual_deal_channel(reason: object | None) -> str | None:
    return _MANUAL_DEAL_REASONS.get(str(reason or "").strip().upper())


def _price(value: object | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _plain(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


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


class Day36ManualMt5ReconciliationService:
    """Mirror proven outside-app changes without ever fighting the broker/user."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        gateway: MetaApiReadGateway,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway

    async def reconcile(self, user_id: UUID) -> Day36ReconciliationResult:
        account = self._account(user_id)
        if account is None:
            return Day36ReconciliationResult(
                user_id=user_id,
                stop_loss_changes=0,
                take_profit_changes=0,
                manual_closes=0,
                actions=self.read_recent(user_id),
            )
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
        except BrokerCredentialDecryptionError as exc:
            raise Day36ReconciliationError("broker_credential_decryption_failed") from exc

        try:
            region = await self._gateway.resolve_account_region(
                token=token,
                account_id=account.account_id,
            )
            broker_payloads = await self._gateway.read_positions(
                token=token,
                account_id=account.account_id,
                region=region,
            )
        except MetaApiGatewayError as exc:
            raise Day36ReconciliationError(exc.code, retryable=exc.retryable) from exc

        broker_positions = {
            str(item.get("id") or "").strip(): item
            for item in broker_payloads
            if str(item.get("id") or "").strip()
        }
        local_positions = self._mapped_positions(user_id)
        sl_changes = 0
        tp_changes = 0
        manual_closes = 0
        observed_at = datetime.now(UTC)

        for position in local_positions:
            if position.status != "open":
                continue
            broker = broker_positions.get(position.broker_position_id)
            if broker is not None:
                broker_updated_at = _broker_time(
                    broker.get("updateTime") or broker.get("time"),
                    observed_at,
                )
                broker_sl = _price(broker.get("stopLoss"))
                broker_tp = _price(broker.get("takeProfit"))
                if broker_sl != position.stop_loss and self._record_price_change(
                    user_id=user_id,
                    position=position,
                    field="stop_loss",
                    old_value=position.stop_loss,
                    new_value=broker_sl,
                    occurred_at=broker_updated_at,
                ):
                    sl_changes += 1
                if broker_tp != position.take_profit and self._record_price_change(
                    user_id=user_id,
                    position=position,
                    field="take_profit",
                    old_value=position.take_profit,
                    new_value=broker_tp,
                    occurred_at=broker_updated_at,
                ):
                    tp_changes += 1
                continue

            manual_exit = await self._manual_exit_deal(
                token=token,
                account_id=account.account_id,
                region=region,
                broker_position_id=position.broker_position_id,
                fallback=observed_at,
            )
            if manual_exit is not None and self._record_manual_close(
                user_id=user_id,
                position=position,
                deal=manual_exit,
                observed_at=observed_at,
            ):
                manual_closes += 1

        # A Day 32/34 read may have reconciled a missing position before this endpoint
        # observed it. Backfill the explicit manual label from the immutable broker deal
        # ledger when strong CLIENT/MOBILE/WEB evidence arrives later.
        manual_closes += self._backfill_manual_closes(user_id)

        return Day36ReconciliationResult(
            user_id=user_id,
            stop_loss_changes=sl_changes,
            take_profit_changes=tp_changes,
            manual_closes=manual_closes,
            actions=self.read_recent(user_id),
        )

    def _account(self, user_id: UUID) -> _Account | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id, metaapi_token_ciphertext, status
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status!='revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None or str(row["status"]) != "connected":
            return None
        return _Account(
            account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )

    def _mapped_positions(self, user_id: UUID) -> tuple[_MappedPosition, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, signal_id, tp_index, broker_position_id, status,
                           stop_loss, take_profit
                    FROM positions
                    WHERE user_id=:user_id
                      AND broker_position_id IS NOT NULL
                      AND status IN ('open','closed')
                    ORDER BY created_at, tp_index, id
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        return tuple(
            _MappedPosition(
                id=row["id"],
                signal_id=row["signal_id"],
                tp_index=int(row["tp_index"]),
                broker_position_id=str(row["broker_position_id"]),
                status=str(row["status"]),
                stop_loss=_price(row["stop_loss"]),
                take_profit=_price(row["take_profit"]),
            )
            for row in rows
        )

    async def _manual_exit_deal(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        broker_position_id: str,
        fallback: datetime,
    ) -> dict[str, Any] | None:
        try:
            deals = await self._gateway.read_deals_by_position(
                token=token,
                account_id=account_id,
                region=region,
                position_id=broker_position_id,
            )
        except MetaApiGatewayError as exc:
            if exc.retryable:
                return None
            return None
        exits = [
            item
            for item in deals
            if str(item.get("entryType") or "").upper() in _EXIT_ENTRY_TYPES
            and manual_deal_channel(item.get("reason")) is not None
        ]
        if not exits:
            return None
        exits.sort(key=lambda item: _broker_time(item.get("time"), fallback))
        return exits[-1]

    def _record_price_change(
        self,
        *,
        user_id: UUID,
        position: _MappedPosition,
        field: str,
        old_value: Decimal | None,
        new_value: Decimal | None,
        occurred_at: datetime,
    ) -> bool:
        if field not in {"stop_loss", "take_profit"}:
            raise ValueError("day36_price_field_invalid")
        action_type = "stop_loss_changed" if field == "stop_loss" else "take_profit_changed"
        identity = public_trade_identity(position.signal_id)
        field_label = "Stop loss" if field == "stop_loss" else "Take profit"
        detail = (
            f"{identity.reference} · TP{position.tp_index} · {field_label} "
            f"{_plain(old_value) or 'Not set'} → {_plain(new_value) or 'Not set'}"
        )
        now = datetime.now(UTC)
        with self._session_factory() as session:
            updated = session.execute(
                text(
                    f"""
                    UPDATE positions
                    SET {field}=:new_value, updated_at=:now
                    WHERE id=:position_id
                      AND user_id=:user_id
                      AND status='open'
                      AND {field} IS NOT DISTINCT FROM :old_value
                    RETURNING id
                    """
                ),
                {
                    "new_value": new_value,
                    "old_value": old_value,
                    "now": now,
                    "position_id": position.id,
                    "user_id": user_id,
                },
            ).scalar_one_or_none()
            if updated is None:
                session.rollback()
                return False
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.day36_manual_action",
                    entity_type="position",
                    entity_id=position.id,
                    payload={
                        "label": MANUAL_ACTION_LABEL,
                        "action_type": action_type,
                        "field": field,
                        "old_value": _plain(old_value),
                        "new_value": _plain(new_value),
                        "broker_position_id": position.broker_position_id,
                        "signal_id": str(position.signal_id),
                        "tp_index": position.tp_index,
                        "trade_reference": identity.reference,
                        "occurred_at": occurred_at.isoformat(),
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
        return True

    def _record_manual_close(
        self,
        *,
        user_id: UUID,
        position: _MappedPosition,
        deal: dict[str, Any],
        observed_at: datetime,
    ) -> bool:
        channel = manual_deal_channel(deal.get("reason"))
        if channel is None:
            return False
        identity = public_trade_identity(position.signal_id)
        occurred_at = _broker_time(deal.get("time"), observed_at)
        exit_price = _price(deal.get("price"))
        detail = f"{identity.reference} · TP{position.tp_index} · Open → Closed via {channel}"
        with self._session_factory() as session:
            exists = session.execute(
                text(
                    """
                    SELECT 1
                    FROM audit_events
                    WHERE actor_user_id=:user_id
                      AND event_type='mt5.day36_manual_action'
                      AND entity_type='position'
                      AND entity_id=:position_id
                      AND payload->>'action_type'='position_closed'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id, "position_id": position.id},
            ).scalar_one_or_none()
            if exists:
                return False
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='closed',
                        closed_at=COALESCE(closed_at,:occurred_at),
                        exit_price=COALESCE(exit_price,:exit_price),
                        close_reason=CASE
                            WHEN close_reason IS NULL
                              OR close_reason IN ('external_close','broker_settled')
                            THEN 'manual_external_close'
                            ELSE close_reason
                        END,
                        updated_at=:observed_at
                    WHERE id=:position_id AND user_id=:user_id
                    """
                ),
                {
                    "position_id": position.id,
                    "user_id": user_id,
                    "occurred_at": occurred_at,
                    "exit_price": exit_price,
                    "observed_at": observed_at,
                },
            )
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.day36_manual_action",
                    entity_type="position",
                    entity_id=position.id,
                    payload={
                        "label": MANUAL_ACTION_LABEL,
                        "action_type": "position_closed",
                        "old_value": "open",
                        "new_value": "closed",
                        "broker_reason": str(deal.get("reason") or ""),
                        "manual_channel": channel,
                        "broker_position_id": position.broker_position_id,
                        "signal_id": str(position.signal_id),
                        "tp_index": position.tp_index,
                        "trade_reference": identity.reference,
                        "occurred_at": occurred_at.isoformat(),
                        "exit_price": _plain(exit_price),
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()
        return True

    def _backfill_manual_closes(self, user_id: UUID) -> int:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT p.id, p.signal_id, p.tp_index, p.broker_position_id,
                           p.status, p.stop_loss, p.take_profit,
                           d.raw_payload, d.occurred_at
                    FROM positions AS p
                    JOIN LATERAL (
                        SELECT bd.raw_payload, bd.occurred_at
                        FROM broker_deals AS bd
                        WHERE bd.position_id=p.id
                          AND bd.entry_type IN ('DEAL_ENTRY_OUT','DEAL_ENTRY_OUT_BY')
                          AND bd.raw_payload->>'reason' IN (
                              'DEAL_REASON_CLIENT','DEAL_REASON_MOBILE','DEAL_REASON_WEB'
                          )
                        ORDER BY bd.occurred_at DESC, bd.broker_deal_id DESC
                        LIMIT 1
                    ) AS d ON TRUE
                    WHERE p.user_id=:user_id
                      AND p.status='closed'
                      AND p.broker_position_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM audit_events AS a
                          WHERE a.actor_user_id=:user_id
                            AND a.event_type='mt5.day36_manual_action'
                            AND a.entity_type='position'
                            AND a.entity_id=p.id
                            AND a.payload->>'action_type'='position_closed'
                      )
                    ORDER BY d.occurred_at
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        created = 0
        for row in rows:
            payload = row["raw_payload"] if isinstance(row["raw_payload"], dict) else {}
            deal = dict(payload)
            if not deal.get("time") and isinstance(row["occurred_at"], datetime):
                deal["time"] = row["occurred_at"].isoformat()
            position = _MappedPosition(
                id=row["id"],
                signal_id=row["signal_id"],
                tp_index=int(row["tp_index"]),
                broker_position_id=str(row["broker_position_id"]),
                status=str(row["status"]),
                stop_loss=_price(row["stop_loss"]),
                take_profit=_price(row["take_profit"]),
            )
            if self._record_manual_close(
                user_id=user_id,
                position=position,
                deal=deal,
                observed_at=datetime.now(UTC),
            ):
                created += 1
        return created

    def read_recent(self, user_id: UUID, *, limit: int = 12) -> tuple[Day36ManualAction, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, entity_id, payload, created_at
                    FROM audit_events
                    WHERE actor_user_id=:user_id
                      AND event_type='mt5.day36_manual_action'
                      AND entity_type='position'
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """
                ),
                {"user_id": user_id, "limit": max(1, min(limit, 50))},
            ).mappings().all()
        result: list[Day36ManualAction] = []
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            occurred_at = _broker_time(payload.get("occurred_at"), row["created_at"])
            action_type = str(payload.get("action_type") or "external_change")
            old_value = str(payload["old_value"]) if payload.get("old_value") is not None else None
            new_value = str(payload["new_value"]) if payload.get("new_value") is not None else None
            trade_reference = str(payload["trade_reference"]) if payload.get("trade_reference") else None
            tp_index = payload.get("tp_index")
            if action_type == "stop_loss_changed":
                detail = f"{trade_reference or 'Trade'} · TP{tp_index} · Stop loss {old_value or 'Not set'} → {new_value or 'Not set'}"
            elif action_type == "take_profit_changed":
                detail = f"{trade_reference or 'Trade'} · TP{tp_index} · Take profit {old_value or 'Not set'} → {new_value or 'Not set'}"
            elif action_type == "position_closed":
                channel = str(payload.get("manual_channel") or "MT5")
                detail = f"{trade_reference or 'Trade'} · TP{tp_index} · Open → Closed via {channel}"
            else:
                detail = f"{trade_reference or 'Trade'} · Outside-app MT5 change"
            result.append(
                Day36ManualAction(
                    audit_id=row["id"],
                    position_id=row["entity_id"],
                    action_type=action_type,
                    label=MANUAL_ACTION_LABEL,
                    detail=detail,
                    old_value=old_value,
                    new_value=new_value,
                    trade_reference=trade_reference,
                    occurred_at=occurred_at,
                )
            )
        return tuple(result)


__all__ = [
    "Day36ManualAction",
    "Day36ManualMt5ReconciliationService",
    "Day36ReconciliationError",
    "Day36ReconciliationResult",
    "MANUAL_ACTION_LABEL",
    "manual_deal_channel",
]
