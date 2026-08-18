"""Reconcile ambiguous broker mutations without retrying a trade order.

A MetaAPI POST timeout is not proof that MT5 rejected the mutation. The request may have
reached the broker while the HTTP response was lost. Blindly retrying would risk duplicate
exposure. The critical execution path already assigns a unique broker client ID to every
planned tranche, so compensation can use that immutable identity instead.

On rollback, inspect both broker positions and pending orders for every planned client ID,
including a plan whose POST never returned an order ID. Any broker artifact found is
closed/cancelled by exact ID. Nothing is re-opened or retried.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError

_installed = False
_AMBIGUOUS_CODES = {
    "metaapi_timeout",
    "metaapi_unreachable",
    "metaapi_temporarily_unavailable",
}
_RECONCILE_ATTEMPTS = 3
_RECONCILE_DELAY_SECONDS = 0.35


def install_ambiguous_trade_reconciliation_override() -> None:
    global _installed
    if _installed:
        return

    from app.paper_critical_execution import PaperCriticalExecutionService

    original = PaperCriticalExecutionService._rollback_critical
    if getattr(original, "_all_client_ids_reconciled", False):
        _installed = True
        return

    async def rollback_critical(
        self: Any,
        *,
        owner_user_id,
        signal,
        account,
        token: str,
        region: str,
        planned,
        submitted,
        reason: str,
    ) -> bool:
        """Compensate returned and hidden broker mutations by planned client ID."""
        attempts = _RECONCILE_ATTEMPTS if reason in _AMBIGUOUS_CODES else 1
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

        closed_ids = set()
        cancelled_ids = set()
        hidden_detected = set()
        unresolved_ids = set()

        for item in reversed(planned):
            returned_order_id = str(submitted.get(item.local_id, "") or "").strip()
            position_id = by_client_position.get(item.client_id, "")
            discovered_order_id = by_client_order.get(item.client_id, "")
            order_id = returned_order_id or discovered_order_id

            if item.local_id not in submitted and (position_id or discovered_order_id):
                hidden_detected.add(item.local_id)

            # A plan with neither a returned nor discovered broker artifact needs no
            # compensating mutation. For an ambiguous timeout, the bounded broker read
            # above is the safety check; the trade request itself is never retried.
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
                known_broker_mutation = (
                    item.local_id in submitted or item.local_id in hidden_detected
                )
                if cleaned:
                    status = "closed"
                    close_reason = "critical_compensating_rollback"
                    closed_at = now
                elif item.local_id in unresolved_ids:
                    status = "error"
                    close_reason = f"critical_rollback_unresolved:{reason}"[:80]
                    closed_at = None
                elif known_broker_mutation:
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
                        SET status=:status, close_reason=:close_reason,
                            closed_at=:closed_at, updated_at=:now
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
                "ambiguous_mutation_reconciled": reason in _AMBIGUOUS_CODES,
                "paper_demo_only": True,
                "automatic_retry": False,
            },
        )
        return len(unresolved_ids) == 0

    rollback_critical._all_client_ids_reconciled = True  # type: ignore[attr-defined]
    PaperCriticalExecutionService._rollback_critical = rollback_critical
    _installed = True


__all__ = ["install_ambiguous_trade_reconciliation_override"]
