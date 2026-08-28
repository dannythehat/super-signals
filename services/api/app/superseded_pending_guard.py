"""Fail-closed cancellation of broker-held pending entries superseded by newer provider signals.

A provider's newer accepted signal for the same symbol invalidates every older pending
entry from that provider. Broker orders are cancelled by exact immutable order ID before
a newer trade is allowed to execute. If broker truth cannot prove the old pending entry
is safely cancelled/terminal, the newer execution is blocked rather than risking
contradictory positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher
from app.mt5_execution_day26 import Day26ExecutionError

_TERMINAL_NO_FILL_STATES = {
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
}
_FILLED_STATES = {"ORDER_STATE_FILLED", "ORDER_STATE_PARTIAL"}


@dataclass(frozen=True, slots=True)
class SupersededPendingResult:
    candidates: int
    cancelled: int
    terminalized: int
    unresolved: int


class SupersededPendingOrderGuard:
    """Cancel superseded broker pending entries using broker-authoritative truth."""

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

    async def cancel_before_signal(
        self,
        *,
        user_id: UUID,
        signal_id: UUID,
        account_environment: str,
    ) -> SupersededPendingResult:
        rows = self._superseded_rows_for_signal(user_id=user_id, signal_id=signal_id)
        result = await self._cancel_rows(
            user_id=user_id,
            rows=rows,
            account_environment=account_environment,
        )
        if result.unresolved:
            raise Day26ExecutionError("superseded_pending_not_proven_safe")
        return result

    async def cleanup_existing(
        self,
        *,
        user_id: UUID,
        account_environment: str,
    ) -> SupersededPendingResult:
        rows = self._all_superseded_rows(user_id=user_id)
        return await self._cancel_rows(
            user_id=user_id,
            rows=rows,
            account_environment=account_environment,
        )

    async def _cancel_rows(
        self,
        *,
        user_id: UUID,
        rows: list[dict[str, object]],
        account_environment: str,
    ) -> SupersededPendingResult:
        if not rows:
            return SupersededPendingResult(0, 0, 0, 0)

        account = self._broker_account(user_id, account_environment)
        if account is None:
            for row in rows:
                self._audit_unresolved(user_id, row, "superseded_pending_account_unavailable")
            return SupersededPendingResult(len(rows), 0, 0, len(rows))
        account_id, ciphertext = account
        try:
            token = self._cipher.decrypt(ciphertext).strip()
        except BrokerCredentialDecryptionError:
            for row in rows:
                self._audit_unresolved(user_id, row, "superseded_pending_decrypt_failed")
            return SupersededPendingResult(len(rows), 0, 0, len(rows))
        if len(token) < 20:
            for row in rows:
                self._audit_unresolved(user_id, row, "superseded_pending_token_invalid")
            return SupersededPendingResult(len(rows), 0, 0, len(rows))

        try:
            region = await self._read_gateway.resolve_account_region(
                token=token,
                account_id=account_id,
            )
            broker_positions = await self._read_gateway.read_positions(
                token=token,
                account_id=account_id,
                region=region,
            )
            broker_orders = await self._read_gateway.read_orders(
                token=token,
                account_id=account_id,
                region=region,
            )
        except MetaApiGatewayError as exc:
            for row in rows:
                self._audit_unresolved(
                    user_id,
                    row,
                    f"superseded_pending_broker_read_failed:{exc.code}",
                )
            return SupersededPendingResult(len(rows), 0, 0, len(rows))

        by_client_position = {
            str(item.get("clientId") or "").strip(): item
            for item in broker_positions
            if str(item.get("clientId") or "").strip()
        }
        by_order_position = {
            str(item.get("orderId") or "").strip(): item
            for item in broker_positions
            if str(item.get("orderId") or "").strip()
        }
        active_order_ids = {
            str(item.get("id") or "").strip()
            for item in broker_orders
            if str(item.get("id") or "").strip()
        }

        cancelled = 0
        terminalized = 0
        unresolved = 0
        for row in rows:
            order_id = str(row.get("broker_order_id") or "").strip()
            client_id = str(row.get("broker_client_id") or "").strip()
            filled_position = by_client_position.get(client_id) or by_order_position.get(order_id)
            if filled_position is not None:
                unresolved += 1
                self._audit_unresolved(user_id, row, "superseded_pending_already_filled")
                continue

            if order_id in active_order_ids:
                try:
                    await self._trade_gateway.cancel_order(
                        token=token,
                        account_id=account_id,
                        region=region,
                        order_id=order_id,
                    )
                except MetaApiGatewayError as exc:
                    unresolved += 1
                    self._audit_unresolved(
                        user_id,
                        row,
                        f"superseded_pending_cancel_failed:{exc.code}",
                    )
                    continue
                self._persist_cancelled(user_id, row, broker_mutation=True)
                cancelled += 1
                continue

            try:
                history = await self._read_gateway.read_history_orders_by_ticket(
                    token=token,
                    account_id=account_id,
                    region=region,
                    order_id=order_id,
                )
            except MetaApiGatewayError as exc:
                unresolved += 1
                self._audit_unresolved(
                    user_id,
                    row,
                    f"superseded_pending_history_failed:{exc.code}",
                )
                continue

            terminal = self._matching_history(row, history)
            if terminal is None:
                unresolved += 1
                self._audit_unresolved(user_id, row, "superseded_pending_broker_state_unknown")
                continue
            state = str(terminal.get("state") or "").strip().upper()
            if state in _TERMINAL_NO_FILL_STATES:
                self._persist_cancelled(user_id, row, broker_mutation=False)
                terminalized += 1
                continue
            if state in _FILLED_STATES:
                unresolved += 1
                self._audit_unresolved(user_id, row, "superseded_pending_already_filled")
                continue
            unresolved += 1
            self._audit_unresolved(
                user_id,
                row,
                f"superseded_pending_history_state:{state or 'missing'}",
            )

        return SupersededPendingResult(
            candidates=len(rows),
            cancelled=cancelled,
            terminalized=terminalized,
            unresolved=unresolved,
        )

    def _superseded_rows_for_signal(
        self,
        *,
        user_id: UUID,
        signal_id: UUID,
    ) -> list[dict[str, object]]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    WITH current_signal AS (
                        SELECT id, source_id, symbol, provider_message_id,
                               source_revision_index, source_posted_at
                        FROM signals
                        WHERE id=:signal_id
                        LIMIT 1
                    )
                    SELECT p.id, p.signal_id, p.broker_client_id, p.broker_order_id,
                           s.source_id, s.symbol, s.side, s.provider_message_id,
                           s.source_revision_index, s.source_posted_at
                    FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    CROSS JOIN current_signal c
                    WHERE p.user_id=:user_id
                      AND p.status='pending'
                      AND p.broker_client_id IS NOT NULL
                      AND p.broker_order_id IS NOT NULL
                      AND s.source_id=c.source_id
                      AND UPPER(s.symbol)=UPPER(c.symbol)
                      AND s.id<>c.id
                      AND (
                            (s.provider_message_id IS NOT NULL
                             AND c.provider_message_id IS NOT NULL
                             AND (
                                  s.provider_message_id < c.provider_message_id
                                  OR (
                                      s.provider_message_id=c.provider_message_id
                                      AND s.source_revision_index < c.source_revision_index
                                  )
                             ))
                            OR (
                                (s.provider_message_id IS NULL OR c.provider_message_id IS NULL)
                                AND s.source_posted_at < c.source_posted_at
                            )
                      )
                    ORDER BY s.source_posted_at, p.created_at
                    """
                ),
                {"user_id": user_id, "signal_id": signal_id},
            ).mappings().all()
        return [dict(row) for row in rows]

    def _all_superseded_rows(self, *, user_id: UUID) -> list[dict[str, object]]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT p.id, p.signal_id, p.broker_client_id, p.broker_order_id,
                           s.source_id, s.symbol, s.side, s.provider_message_id,
                           s.source_revision_index, s.source_posted_at
                    FROM positions p
                    JOIN signals s ON s.id=p.signal_id
                    WHERE p.user_id=:user_id
                      AND p.status='pending'
                      AND p.broker_client_id IS NOT NULL
                      AND p.broker_order_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1
                          FROM signals newer
                          WHERE newer.source_id=s.source_id
                            AND UPPER(newer.symbol)=UPPER(s.symbol)
                            AND newer.parser_status='accepted'
                            AND newer.id<>s.id
                            AND (
                                (s.provider_message_id IS NOT NULL
                                 AND newer.provider_message_id IS NOT NULL
                                 AND (
                                      newer.provider_message_id > s.provider_message_id
                                      OR (
                                          newer.provider_message_id=s.provider_message_id
                                          AND newer.source_revision_index > s.source_revision_index
                                      )
                                 ))
                                OR (
                                    (s.provider_message_id IS NULL OR newer.provider_message_id IS NULL)
                                    AND newer.source_posted_at > s.source_posted_at
                                )
                            )
                      )
                    ORDER BY s.source_posted_at, p.created_at
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        return [dict(row) for row in rows]

    def _broker_account(
        self,
        user_id: UUID,
        account_environment: str,
    ) -> tuple[str, bytes] | None:
        environment = account_environment.strip().lower()
        with self._session_factory() as session:
            if environment == "demo":
                row = session.execute(
                    text(
                        """
                        SELECT metaapi_account_id, metaapi_token_ciphertext
                        FROM mt5_accounts
                        WHERE owner_user_id=:user_id
                          AND status='connected'
                          AND account_environment='demo'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().first()
            elif environment == "live":
                row = session.execute(
                    text(
                        """
                        SELECT m.metaapi_account_id, m.metaapi_token_ciphertext
                        FROM users u
                        JOIN user_roles ur ON ur.user_id=u.id
                        JOIN roles r ON r.id=ur.role_id AND r.name='user'
                        JOIN mt5_accounts m ON m.owner_user_id=u.id
                        JOIN mt5_account_approvals a
                          ON a.user_id=u.id AND a.status='active'
                         AND a.login=m.login AND LOWER(a.server)=LOWER(m.server)
                        WHERE u.id=:user_id
                          AND u.status='active'
                          AND m.status='connected'
                          AND m.account_environment='live'
                        ORDER BY m.created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().first()
            else:
                return None
        if row is None:
            return None
        return str(row["metaapi_account_id"]), bytes(row["metaapi_token_ciphertext"])

    @staticmethod
    def _matching_history(
        row: dict[str, object],
        history: list[dict[str, object]],
    ) -> dict[str, object] | None:
        order_id = str(row.get("broker_order_id") or "").strip()
        client_id = str(row.get("broker_client_id") or "").strip()
        matches = [
            item
            for item in history
            if str(item.get("id") or "").strip() == order_id
            and (
                not str(item.get("clientId") or "").strip()
                or str(item.get("clientId") or "").strip() == client_id
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def _persist_cancelled(
        self,
        user_id: UUID,
        row: dict[str, object],
        *,
        broker_mutation: bool,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='skipped',
                        close_reason='superseded_by_newer_provider_signal',
                        closed_at=COALESCE(closed_at,:now),
                        updated_at=:now
                    WHERE id=:id AND status='pending'
                    """
                ),
                {"id": row["id"], "now": now},
            )
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.superseded_pending_cancelled",
                    entity_type="position",
                    entity_id=row["id"],
                    payload={
                        "broker_authoritative": True,
                        "broker_order_id": str(row.get("broker_order_id") or ""),
                        "source_id": str(row.get("source_id") or ""),
                        "symbol": str(row.get("symbol") or ""),
                        "side": str(row.get("side") or ""),
                        "broker_mutation": broker_mutation,
                        "trade_action_created": broker_mutation,
                        "reason": "newer_provider_signal_superseded_pending_entry",
                    },
                )
            )
            session.commit()

    def _audit_unresolved(
        self,
        user_id: UUID,
        row: dict[str, object],
        code: str,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="mt5.superseded_pending_unresolved",
                    entity_type="position",
                    entity_id=row["id"],
                    payload={
                        "code": code,
                        "broker_order_id": str(row.get("broker_order_id") or ""),
                        "source_id": str(row.get("source_id") or ""),
                        "symbol": str(row.get("symbol") or ""),
                        "side": str(row.get("side") or ""),
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()


__all__ = ["SupersededPendingOrderGuard", "SupersededPendingResult"]
