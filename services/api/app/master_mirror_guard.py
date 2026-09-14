"""Hard Owner -> member exposure mirror guard.

The Owner account is the master for every mapped Super Signals trade. Member accounts
(demo/paper or live) may never open or retain mapped exposure that the Owner does not
have. Broker reads are authoritative; if Owner broker truth is unavailable the guard
fails closed and performs no member mutation.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app import broker_settlement_canonical as _broker_module
from app import member_routing_canonical as _member_module
from app.metaapi_gateway import MetaApiGatewayError
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.performance_ledger_day33 import Day33LedgerError
from app.weekend_trading_freeze import market_week_frozen

logger = logging.getLogger(__name__)

_BaseSettlement = _broker_module.CanonicalBrokerSettlementManager
_OriginalDistribute = _member_module.MemberDistributionService.distribute
_GRACE_SECONDS = 30


def _owner_id(session_factory) -> UUID | None:
    if session_factory is None:
        return None
    with session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT u.id
                FROM users u
                JOIN user_roles ur ON ur.user_id=u.id
                JOIN roles r ON r.id=ur.role_id
                WHERE r.name='owner' AND u.status='active'
                ORDER BY u.created_at
                LIMIT 1
                """
            )
        ).scalar_one_or_none()
    return UUID(str(value)) if value is not None else None


def _owner_has_signal_exposure(session_factory, owner_id: UUID, signal_id: UUID) -> bool:
    with session_factory() as session:
        return bool(
            session.execute(
                text(
                    """
                    SELECT 1
                    FROM positions
                    WHERE user_id=:owner_id
                      AND signal_id=:signal_id
                      AND status IN ('open','pending')
                      AND (broker_position_id IS NOT NULL OR broker_order_id IS NOT NULL)
                    LIMIT 1
                    """
                ),
                {"owner_id": owner_id, "signal_id": signal_id},
            ).scalar_one_or_none()
        )


async def _master_first_distribute(self, *, signal_id: UUID):
    # Test harnesses intentionally use no DB. Preserve their isolated policy checks.
    if getattr(self, "_session_factory", None) is None:
        return await _OriginalDistribute(self, signal_id=signal_id)

    owner_id = _owner_id(self._session_factory)
    targets = self._targets()
    reason = None
    if owner_id is None:
        reason = "master_reference_missing"
    elif not _owner_has_signal_exposure(self._session_factory, owner_id, signal_id):
        reason = "master_not_executed"

    if reason is None:
        return await _OriginalDistribute(self, signal_id=signal_id)

    outcomes = tuple(
        _member_module.MemberDistributionOutcome(
            user_id=t.user_id,
            outcome="skipped",
            risk_percent=t.risk_percent,
            allow_double_lot=t.allow_double_lot,
            position_count=0,
            volume_per_position=(),
            error_code=reason,
        )
        for t in targets
    )
    for outcome in outcomes:
        self._audit_user(signal_id=signal_id, outcome=outcome)
    result = _member_module.MemberDistributionResult(
        signal_id=signal_id,
        target_count=len(targets),
        executed_count=0,
        skipped_count=len(outcomes),
        outcomes=outcomes,
    )
    self._audit_summary(result)
    with self._session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="mt5.master_mirror_member_distribution_blocked",
                entity_type="signal",
                entity_id=signal_id,
                payload={
                    "reason": reason,
                    "target_count": len(targets),
                    "master_first": True,
                    "member_only_exposure_forbidden": True,
                    "trade_action_created": False,
                },
            )
        )
        session.commit()
    return result


def _match(
    items: list[dict[str, object]],
    broker_id: object | None,
    client_id: object | None,
) -> dict[str, object] | None:
    wanted_id = str(broker_id or "").strip()
    wanted_client = str(client_id or "").strip()
    for item in items:
        if wanted_id and str(item.get("id") or "").strip() == wanted_id:
            return item
        if wanted_client and str(item.get("clientId") or "").strip() == wanted_client:
            return item
    return None


class MasterMirrorSettlementManager(_BaseSettlement):
    """Continuously remove mapped member exposure absent from the Owner."""

    async def poll_once(self):  # noqa: ANN201
        # Base still owns _has_unsettled_mapped_positions, sync_user and
        # flat_account_truth_sync_complete. This wrapper only adds mirror enforcement.
        result = await super().poll_once()
        if market_week_frozen():
            return result
        try:
            await self._enforce_master_mirror()
        except Exception:
            logger.exception("Master mirror reconciliation failed safely")
        return result

    def _account(self, user_id: UUID) -> tuple[str, bytes] | None:
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

    async def _broker_state(
        self,
        account_id: str,
        ciphertext: bytes,
    ) -> tuple[str, str, list[dict[str, object]], list[dict[str, object]]]:
        if self._cipher is None or self._read is None:
            raise RuntimeError("mirror_reader_unavailable")
        token = self._cipher.decrypt(ciphertext)
        region = await self._read.resolve_account_region(token=token, account_id=account_id)
        positions = await self._read.read_positions(
            token=token, account_id=account_id, region=region
        )
        orders = await self._read.read_orders(
            token=token, account_id=account_id, region=region
        )
        return token, region, positions, orders

    def _owner_active_keys(
        self,
        owner_id: UUID,
        positions: list[dict[str, object]],
        orders: list[dict[str, object]],
    ) -> set[tuple[UUID, int]]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT signal_id,tp_index,status,broker_position_id,
                           broker_order_id,broker_client_id
                    FROM positions
                    WHERE user_id=:owner_id AND status IN ('open','pending')
                    """
                ),
                {"owner_id": owner_id},
            ).mappings().all()

        active: set[tuple[UUID, int]] = set()
        for row in rows:
            broker = _match(
                positions if str(row["status"]) == "open" else orders,
                row["broker_position_id"]
                if str(row["status"]) == "open"
                else row["broker_order_id"],
                row["broker_client_id"],
            )
            if broker is not None:
                active.add((UUID(str(row["signal_id"])), int(row["tp_index"])))
        return active

    def _member_rows(self, owner_id: UUID) -> list[Any]:
        with self._session_factory() as session:
            return list(
                session.execute(
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
                        SELECT p.id,p.user_id,p.signal_id,p.tp_index,p.status,
                               p.broker_position_id,p.broker_order_id,p.broker_client_id,
                               a.metaapi_account_id,a.metaapi_token_ciphertext
                        FROM positions p
                        JOIN latest_accounts a ON a.owner_user_id=p.user_id
                        WHERE p.user_id<>:owner_id
                          AND p.status IN ('open','pending')
                          AND p.created_at < now() - (:grace * interval '1 second')
                          AND (p.broker_position_id IS NOT NULL OR p.broker_order_id IS NOT NULL)
                          AND EXISTS (
                              SELECT 1
                              FROM user_roles ur
                              JOIN roles r ON r.id=ur.role_id
                              WHERE ur.user_id=p.user_id AND r.name='user'
                          )
                        ORDER BY p.user_id,p.signal_id,p.tp_index,p.id
                        """
                    ),
                    {"owner_id": owner_id, "grace": _GRACE_SECONDS},
                ).mappings().all()
            )

    def _mark_terminal(
        self,
        row: Any,
        *,
        owner_id: UUID,
        broker_action_sent: bool,
    ) -> None:
        expected = str(row["status"])
        terminal = "closed" if expected == "open" else "cancelled"
        reason = (
            "master_mirror_guard_closed"
            if expected == "open"
            else "master_mirror_guard_cancelled"
        )
        event_type = (
            "mt5.master_mirror_orphan_position_closed"
            if expected == "open"
            else "mt5.master_mirror_orphan_order_cancelled"
        )
        now = datetime.now(UTC)
        with self._session_factory() as session:
            changed = session.execute(
                text(
                    """
                    UPDATE positions
                    SET status=:terminal,
                        closed_at=COALESCE(closed_at,:now),
                        close_reason=COALESCE(close_reason,:reason),
                        updated_at=:now
                    WHERE id=:position_id AND status=:expected
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
            if changed is not None:
                session.add(
                    AuditEvent(
                        actor_user_id=UUID(str(row["user_id"])),
                        event_type=event_type,
                        entity_type="position",
                        entity_id=UUID(str(row["id"])),
                        payload={
                            "master_user_id": str(owner_id),
                            "signal_id": str(row["signal_id"]),
                            "tp_index": int(row["tp_index"]),
                            "broker_action_sent": broker_action_sent,
                            "master_first": True,
                            "member_only_exposure_forbidden": True,
                        },
                    )
                )
            session.commit()

    def _audit_failure(self, row: Any, owner_id: UUID, code: str) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=UUID(str(row["user_id"])),
                    event_type="mt5.master_mirror_reconcile_failed",
                    entity_type="position",
                    entity_id=UUID(str(row["id"])),
                    payload={
                        "master_user_id": str(owner_id),
                        "signal_id": str(row["signal_id"]),
                        "tp_index": int(row["tp_index"]),
                        "status": str(row["status"]),
                        "error_code": code,
                        "retry_on_next_poll": True,
                    },
                )
            )
            session.commit()

    async def _enforce_master_mirror(self) -> None:
        if self._cipher is None or self._read is None or self._trade is None:
            return

        owner_id = _owner_id(self._session_factory)
        if owner_id is None:
            return
        owner_account = self._account(owner_id)
        if owner_account is None:
            return

        try:
            _, _, owner_positions, owner_orders = await self._broker_state(*owner_account)
        except (BrokerCredentialDecryptionError, MetaApiGatewayError, RuntimeError):
            # Never mass-close members when Owner broker truth is unavailable.
            return

        master_active = self._owner_active_keys(owner_id, owner_positions, owner_orders)
        grouped: dict[tuple[UUID, str, bytes], list[Any]] = defaultdict(list)
        for row in self._member_rows(owner_id):
            grouped[
                (
                    UUID(str(row["user_id"])),
                    str(row["metaapi_account_id"]),
                    bytes(row["metaapi_token_ciphertext"]),
                )
            ].append(row)

        for (user_id, account_id, ciphertext), rows in grouped.items():
            try:
                token, region, positions, orders = await self._broker_state(
                    account_id, ciphertext
                )
            except (BrokerCredentialDecryptionError, MetaApiGatewayError, RuntimeError) as exc:
                code = str(getattr(exc, "code", type(exc).__name__))
                for row in rows:
                    key = (UUID(str(row["signal_id"])), int(row["tp_index"]))
                    if key not in master_active:
                        self._audit_failure(row, owner_id, code)
                continue

            attempted: list[tuple[Any, bool]] = []
            for row in rows:
                key = (UUID(str(row["signal_id"])), int(row["tp_index"]))
                if key in master_active:
                    continue

                status = str(row["status"])
                broker = _match(
                    positions if status == "open" else orders,
                    row["broker_position_id"] if status == "open" else row["broker_order_id"],
                    row["broker_client_id"],
                )
                try:
                    if broker is not None:
                        broker_id = str(broker.get("id") or "").strip()
                        if not broker_id:
                            raise MetaApiGatewayError("broker_mapping_missing")
                        if status == "open":
                            await self._trade.close_position(
                                token=token,
                                account_id=account_id,
                                region=region,
                                position_id=broker_id,
                            )
                        else:
                            await self._trade.cancel_order(
                                token=token,
                                account_id=account_id,
                                region=region,
                                order_id=broker_id,
                            )
                    attempted.append((row, broker is not None))
                except MetaApiGatewayError as exc:
                    self._audit_failure(row, owner_id, exc.code)

            if not attempted:
                continue

            try:
                _, _, after_positions, after_orders = await self._broker_state(
                    account_id, ciphertext
                )
            except (BrokerCredentialDecryptionError, MetaApiGatewayError, RuntimeError) as exc:
                code = f"post_verify:{getattr(exc, 'code', type(exc).__name__)}"
                for row, _ in attempted:
                    self._audit_failure(row, owner_id, code)
                continue

            changed = False
            for row, action_sent in attempted:
                status = str(row["status"])
                still_active = _match(
                    after_positions if status == "open" else after_orders,
                    row["broker_position_id"] if status == "open" else row["broker_order_id"],
                    row["broker_client_id"],
                )
                if still_active is not None:
                    self._audit_failure(
                        row, owner_id, "broker_exposure_still_active_after_reconcile"
                    )
                    continue
                self._mark_terminal(
                    row, owner_id=owner_id, broker_action_sent=action_sent
                )
                changed = True

            if changed:
                try:
                    await self._performance.sync_user(user_id)
                except Day33LedgerError:
                    logger.warning(
                        "Master mirror performance refresh deferred user=%s", user_id
                    )


_member_module.MemberDistributionService.distribute = _master_first_distribute
_broker_module.CanonicalBrokerSettlementManager = MasterMirrorSettlementManager

__all__ = ["MasterMirrorSettlementManager", "_master_first_distribute"]
