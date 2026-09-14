"""Master-first mirror guard for Super Signals.

The Owner/reference MT5 account is the execution authority for every mapped Super
Signals trade lifecycle. Ordinary member accounts may never carry mapped exposure
for a signal leg that is not also active on the Owner account.

This guard is deliberately broker-confirmed and fail-closed:
* new member distribution is blocked unless the Owner has a mapped active leg;
* every settlement poll compares broker-confirmed Owner exposure with member
  broker exposure and closes/cancels member-only mapped exposure;
* if the Owner broker state cannot be read, no member mutation is attempted;
* manual/unmapped broker positions are never touched.

The guard applies to demo/paper and live accounts identically. Account environment
changes sizing/execution mechanics, never who is authoritative.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app import broker_settlement_canonical as _broker_settlement_module
from app import member_routing_canonical as _member_routing_module
from app.metaapi_gateway import MetaApiGatewayError
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.performance_ledger_day33 import Day33LedgerError
from app.weekend_trading_freeze import market_week_frozen

logger = logging.getLogger(__name__)

_BaseSettlementManager = _broker_settlement_module.CanonicalBrokerSettlementManager
_OriginalMemberDistribute = _member_routing_module.MemberDistributionService.distribute

_RECONCILE_GRACE_SECONDS = 30


def _owner_user_id(session_factory) -> UUID | None:
    with session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT u.id
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id=u.id
                JOIN roles AS r ON r.id=ur.role_id
                WHERE r.name='owner' AND u.status='active'
                ORDER BY u.created_at
                LIMIT 1
                """
            )
        ).scalar_one_or_none()
    return UUID(str(value)) if value is not None else None


def _owner_has_mapped_exposure(session_factory, owner_user_id: UUID, signal_id: UUID) -> bool:
    with session_factory() as session:
        return bool(
            session.execute(
                text(
                    """
                    SELECT 1
                    FROM positions
                    WHERE user_id=:owner_user_id
                      AND signal_id=:signal_id
                      AND status IN ('open','pending')
                      AND (
                          broker_position_id IS NOT NULL
                          OR broker_order_id IS NOT NULL
                      )
                    LIMIT 1
                    """
                ),
                {"owner_user_id": owner_user_id, "signal_id": signal_id},
            ).scalar_one_or_none()
        )


def _record_distribution_gate(
    session_factory,
    *,
    owner_user_id: UUID | None,
    signal_id: UUID,
    reason: str,
    target_count: int,
) -> None:
    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_user_id,
                event_type="mt5.master_mirror_member_distribution_blocked",
                entity_type="signal",
                entity_id=signal_id,
                payload={
                    "reason": reason,
                    "target_count": target_count,
                    "master_first": True,
                    "member_only_exposure_forbidden": True,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()


async def _master_first_member_distribute(self, *, signal_id: UUID):
    """Do not let any member open a trade the Owner did not successfully map first."""
    owner_user_id = _owner_user_id(self._session_factory)
    targets = self._targets()

    if owner_user_id is None:
        reason = "master_reference_missing"
    elif not _owner_has_mapped_exposure(
        self._session_factory,
        owner_user_id,
        signal_id,
    ):
        reason = "master_not_executed"
    else:
        return await _OriginalMemberDistribute(self, signal_id=signal_id)

    outcomes = tuple(
        _member_routing_module.MemberDistributionOutcome(
            user_id=target.user_id,
            outcome="skipped",
            risk_percent=target.risk_percent,
            allow_double_lot=target.allow_double_lot,
            position_count=0,
            volume_per_position=(),
            error_code=reason,
        )
        for target in targets
    )
    for outcome in outcomes:
        self._audit_user(signal_id=signal_id, outcome=outcome)

    result = _member_routing_module.MemberDistributionResult(
        signal_id=signal_id,
        target_count=len(targets),
        executed_count=0,
        skipped_count=len(outcomes),
        outcomes=outcomes,
    )
    self._audit_summary(result)
    _record_distribution_gate(
        self._session_factory,
        owner_user_id=owner_user_id,
        signal_id=signal_id,
        reason=reason,
        target_count=len(targets),
    )
    return result


def _broker_item(
    items: list[dict[str, object]],
    *,
    broker_id: str | None,
    client_id: str | None,
) -> dict[str, object] | None:
    normalized_id = str(broker_id or "").strip()
    normalized_client = str(client_id or "").strip()
    for item in items:
        item_id = str(item.get("id") or "").strip()
        item_client = str(item.get("clientId") or "").strip()
        if normalized_id and item_id == normalized_id:
            return item
        if normalized_client and item_client == normalized_client:
            return item
    return None


class MasterMirrorSettlementManager(_BaseSettlementManager):
    """Settlement manager with a hard Owner->member exposure invariant."""

    async def poll_once(self):  # noqa: ANN201
        result = await super().poll_once()
        if market_week_frozen():
            return result
        try:
            await self._reconcile_master_mirror()
        except Exception:
            # A mirror read/reconcile failure must never crash settlement or Telegram.
            # Crucially, unexpected failure also never authorizes a blind member close.
            logger.exception("Master mirror reconciliation failed safely")
        return result

    def _connected_account(self, user_id: UUID) -> tuple[str, bytes] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id,metaapi_token_ciphertext
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND status='connected'
                      AND metaapi_account_id IS NOT NULL
                      AND metaapi_token_ciphertext IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return None
        return str(row["metaapi_account_id"]), bytes(row["metaapi_token_ciphertext"])

    async def _read_account_exposure(
        self,
        *,
        account_id: str,
        ciphertext: bytes,
    ) -> tuple[str, str, list[dict[str, object]], list[dict[str, object]]]:
        if self._cipher is None or self._read is None:
            raise RuntimeError("master_mirror_broker_reader_unavailable")
        token = self._cipher.decrypt(ciphertext)
        region = await self._read.resolve_account_region(
            token=token,
            account_id=account_id,
        )
        positions = await self._read.read_positions(
            token=token,
            account_id=account_id,
            region=region,
        )
        orders = await self._read.read_orders(
            token=token,
            account_id=account_id,
            region=region,
        )
        return token, region, positions, orders

    def _master_active_keys(
        self,
        *,
        owner_user_id: UUID,
        broker_positions: list[dict[str, object]],
        broker_orders: list[dict[str, object]],
    ) -> set[tuple[UUID, int]]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT signal_id,tp_index,status,broker_position_id,
                           broker_order_id,broker_client_id
                    FROM positions
                    WHERE user_id=:user_id
                      AND status IN ('open','pending')
                    """
                ),
                {"user_id": owner_user_id},
            ).mappings().all()

        active: set[tuple[UUID, int]] = set()
        for row in rows:
            status = str(row["status"])
            broker = (
                _broker_item(
                    broker_positions,
                    broker_id=(
                        str(row["broker_position_id"])
                        if row["broker_position_id"] is not None
                        else None
                    ),
                    client_id=(
                        str(row["broker_client_id"])
                        if row["broker_client_id"] is not None
                        else None
                    ),
                )
                if status == "open"
                else _broker_item(
                    broker_orders,
                    broker_id=(
                        str(row["broker_order_id"])
                        if row["broker_order_id"] is not None
                        else None
                    ),
                    client_id=(
                        str(row["broker_client_id"])
                        if row["broker_client_id"] is not None
                        else None
                    ),
                )
            )
            if broker is not None:
                active.add((UUID(str(row["signal_id"])), int(row["tp_index"])))
        return active

    def _member_active_rows(self, owner_user_id: UUID) -> list[Any]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    WITH latest_accounts AS (
                        SELECT DISTINCT ON (owner_user_id)
                            owner_user_id,metaapi_account_id,metaapi_token_ciphertext
                        FROM mt5_accounts
                        WHERE status='connected'
                          AND metaapi_account_id IS NOT NULL
                          AND metaapi_token_ciphertext IS NOT NULL
                        ORDER BY owner_user_id,created_at DESC
                    )
                    SELECT
                        p.id,p.user_id,p.signal_id,p.tp_index,p.status,
                        p.broker_position_id,p.broker_order_id,p.broker_client_id,
                        a.metaapi_account_id,a.metaapi_token_ciphertext
                    FROM positions AS p
                    JOIN latest_accounts AS a ON a.owner_user_id=p.user_id
                    WHERE p.user_id<>:owner_user_id
                      AND p.status IN ('open','pending')
                      AND p.created_at < now() - (:grace_seconds * interval '1 second')
                      AND (
                          p.broker_position_id IS NOT NULL
                          OR p.broker_order_id IS NOT NULL
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM user_roles AS ur
                          JOIN roles AS r ON r.id=ur.role_id
                          WHERE ur.user_id=p.user_id
                            AND r.name='user'
                      )
                    ORDER BY p.user_id,p.signal_id,p.tp_index,p.id
                    """
                ),
                {
                    "owner_user_id": owner_user_id,
                    "grace_seconds": _RECONCILE_GRACE_SECONDS,
                },
            ).mappings().all()
        return list(rows)

    def _confirm_orphan_terminal(
        self,
        *,
        row: Any,
        owner_user_id: UUID,
        broker_action_sent: bool,
    ) -> None:
        now = datetime.now(UTC)
        expected = str(row["status"])
        terminal = "closed" if expected == "open" else "cancelled"
        event_type = (
            "mt5.master_mirror_orphan_position_closed"
            if expected == "open"
            else "mt5.master_mirror_orphan_order_cancelled"
        )
        reason = (
            "master_mirror_guard_closed"
            if expected == "open"
            else "master_mirror_guard_cancelled"
        )
        with self._session_factory() as session:
            updated = session.execute(
                text(
                    """
                    UPDATE positions
                    SET status=:terminal,
                        closed_at=COALESCE(closed_at,:now),
                        close_reason=COALESCE(close_reason,:reason),
                        updated_at=:now
                    WHERE id=:position_id
                      AND status=:expected
                    RETURNING id
                    """
                ),
                {
                    "terminal": terminal,
                    "now": now,
                    "reason": reason,
                    "position_id": row["id"],
                    "expected": expected,
                },
            ).scalar_one_or_none()
            if updated is not None:
                session.add(
                    AuditEvent(
                        actor_user_id=UUID(str(row["user_id"])),
                        event_type=event_type,
                        entity_type="position",
                        entity_id=UUID(str(row["id"])),
                        payload={
                            "master_user_id": str(owner_user_id),
                            "signal_id": str(row["signal_id"]),
                            "tp_index": int(row["tp_index"]),
                            "previous_status": expected,
                            "broker_action_sent": broker_action_sent,
                            "member_only_exposure_forbidden": True,
                            "master_first": True,
                        },
                    )
                )
            session.commit()

    def _audit_reconcile_failure(
        self,
        *,
        row: Any,
        owner_user_id: UUID,
        code: str,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=UUID(str(row["user_id"])),
                    event_type="mt5.master_mirror_reconcile_failed",
                    entity_type="position",
                    entity_id=UUID(str(row["id"])),
                    payload={
                        "master_user_id": str(owner_user_id),
                        "signal_id": str(row["signal_id"]),
                        "tp_index": int(row["tp_index"]),
                        "status": str(row["status"]),
                        "error_code": code,
                        "retry_on_next_poll": True,
                        "member_only_exposure_forbidden": True,
                    },
                )
            )
            session.commit()

    async def _reconcile_master_mirror(self) -> None:
        if self._cipher is None or self._read is None or self._trade is None:
            return

        owner_user_id = _owner_user_id(self._session_factory)
        if owner_user_id is None:
            logger.error("Master mirror skipped: active Owner user is missing")
            return

        owner_account = self._connected_account(owner_user_id)
        if owner_account is None:
            logger.error("Master mirror skipped: Owner broker account is unavailable")
            return

        try:
            _, _, owner_positions, owner_orders = await self._read_account_exposure(
                account_id=owner_account[0],
                ciphertext=owner_account[1],
            )
        except (BrokerCredentialDecryptionError, MetaApiGatewayError, RuntimeError) as exc:
            logger.error(
                "Master mirror skipped: Owner broker truth unavailable code=%s",
                getattr(exc, "code", type(exc).__name__),
            )
            return

        # This set is broker-confirmed. If the Owner read fails, we return above and
        # deliberately perform zero member mutations.
        master_active = self._master_active_keys(
            owner_user_id=owner_user_id,
            broker_positions=owner_positions,
            broker_orders=owner_orders,
        )

        rows = self._member_active_rows(owner_user_id)
        grouped: dict[tuple[UUID, str, bytes], list[Any]] = defaultdict(list)
        for row in rows:
            key = (
                UUID(str(row["user_id"])),
                str(row["metaapi_account_id"]),
                bytes(row["metaapi_token_ciphertext"]),
            )
            grouped[key].append(row)

        for (user_id, account_id, ciphertext), member_rows in grouped.items():
            try:
                token, region, member_positions, member_orders = (
                    await self._read_account_exposure(
                        account_id=account_id,
                        ciphertext=ciphertext,
                    )
                )
            except (BrokerCredentialDecryptionError, MetaApiGatewayError, RuntimeError) as exc:
                code = getattr(exc, "code", type(exc).__name__)
                for row in member_rows:
                    if (UUID(str(row["signal_id"])), int(row["tp_index"])) not in master_active:
                        self._audit_reconcile_failure(
                            row=row,
                            owner_user_id=owner_user_id,
                            code=str(code),
                        )
                continue

            attempted: list[tuple[Any, bool]] = []
            for row in member_rows:
                leg_key = (UUID(str(row["signal_id"])), int(row["tp_index"]))
                if leg_key in master_active:
                    continue

                status = str(row["status"])
                try:
                    if status == "open":
                        broker = _broker_item(
                            member_positions,
                            broker_id=(
                                str(row["broker_position_id"])
                                if row["broker_position_id"] is not None
                                else None
                            ),
                            client_id=(
                                str(row["broker_client_id"])
                                if row["broker_client_id"] is not None
                                else None
                            ),
                        )
                        action_sent = broker is not None
                        if broker is not None:
                            broker_id = str(broker.get("id") or "").strip()
                            if not broker_id:
                                raise MetaApiGatewayError(
                                    "broker_position_mapping_missing"
                                )
                            await self._trade.close_position(
                                token=token,
                                account_id=account_id,
                                region=region,
                                position_id=broker_id,
                            )
                    else:
                        broker = _broker_item(
                            member_orders,
                            broker_id=(
                                str(row["broker_order_id"])
                                if row["broker_order_id"] is not None
                                else None
                            ),
                            client_id=(
                                str(row["broker_client_id"])
                                if row["broker_client_id"] is not None
                                else None
                            ),
                        )
                        action_sent = broker is not None
                        if broker is not None:
                            broker_id = str(broker.get("id") or "").strip()
                            if not broker_id:
                                raise MetaApiGatewayError(
                                    "broker_order_mapping_missing"
                                )
                            await self._trade.cancel_order(
                                token=token,
                                account_id=account_id,
                                region=region,
                                order_id=broker_id,
                            )
                    attempted.append((row, action_sent))
                except MetaApiGatewayError as exc:
                    self._audit_reconcile_failure(
                        row=row,
                        owner_user_id=owner_user_id,
                        code=exc.code,
                    )

            if not attempted:
                continue

            # One post-mutation broker read proves the orphan is genuinely gone before
            # the local row is made terminal.
            try:
                _, _, after_positions, after_orders = await self._read_account_exposure(
                    account_id=account_id,
                    ciphertext=ciphertext,
                )
            except (BrokerCredentialDecryptionError, MetaApiGatewayError, RuntimeError) as exc:
                code = getattr(exc, "code", type(exc).__name__)
                for row, _ in attempted:
                    self._audit_reconcile_failure(
                        row=row,
                        owner_user_id=owner_user_id,
                        code=f"post_mutation_verify:{code}",
                    )
                continue

            changed = False
            for row, action_sent in attempted:
                status = str(row["status"])
                still_active = _broker_item(
                    after_positions if status == "open" else after_orders,
                    broker_id=(
                        str(row["broker_position_id"])
                        if status == "open" and row["broker_position_id"] is not None
                        else str(row["broker_order_id"])
                        if status == "pending" and row["broker_order_id"] is not None
                        else None
                    ),
                    client_id=(
                        str(row["broker_client_id"])
                        if row["broker_client_id"] is not None
                        else None
                    ),
                )
                if still_active is not None:
                    self._audit_reconcile_failure(
                        row=row,
                        owner_user_id=owner_user_id,
                        code="broker_exposure_still_active_after_reconcile",
                    )
                    continue

                self._confirm_orphan_terminal(
                    row=row,
                    owner_user_id=owner_user_id,
                    broker_action_sent=action_sent,
                )
                changed = True

            if changed:
                try:
                    await self._performance.sync_user(user_id)
                except Day33LedgerError as exc:
                    logger.warning(
                        "Master mirror performance refresh deferred user=%s code=%s",
                        user_id,
                        exc.code,
                    )


# Patch the canonical member fan-out class itself so any existing references inherit the
# master-first gate, then make the already metrics-hardened settlement manager enforce
# broker-confirmed exposure parity continuously.
_member_routing_module.MemberDistributionService.distribute = _master_first_member_distribute
_broker_settlement_module.CanonicalBrokerSettlementManager = MasterMirrorSettlementManager

__all__ = ["MasterMirrorSettlementManager"]
