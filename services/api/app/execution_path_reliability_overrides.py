"""Broker-facing execution reliability corrections from provider paper testing.

A fresh MARKET instruction must reach MT5. Provider entry prices/zones remain evidence
for the signal, sizing and audit trail, but Super Signals must not turn a few ticks of
movement during its own processing into a second trading decision that suppresses the
trade. The broker remains authoritative: if the provider's unchanged SL/TP geometry is
no longer valid, MT5 can reject the actual order and that broker truth is recorded.

Literal LIMIT/STOP/PENDING instructions are unchanged and remain broker-held at the
provider's exact prices. Broker mutation failures still trigger the existing atomic
compensation logic and trade mutations are never blindly retried.

This module also retains the PostgreSQL-safe critical-order persistence correction.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

_installed = False


def _install_fresh_market_submission_policy() -> None:
    """Remove Super Signals' own market-zone submission veto.

    Day 28 historically re-read XAUUSD immediately before a market order and rejected
    the order locally when price had moved outside the provider's textual range. That
    defeated the later fresh-market policy and caused otherwise valid provider signals
    to be rolled back before MT5 had a chance to decide them.

    A zone is set only for market-order guarding. Pending orders use the separate
    PaperPendingOrderGateway and therefore keep their literal broker-side semantics.
    Exact market entries never set a zone and continue through the original wrapper.
    """
    from app.day28_zone_guard import Day28ZoneGuardTradeGateway

    original_place = Day28ZoneGuardTradeGateway.place_market_order
    if getattr(original_place, "_fresh_market_reaches_broker", False):
        return

    async def place_market_order(self: Any, **kwargs: Any):
        zone = self._zone.get()
        if zone is None:
            return await original_place(self, **kwargs)

        # This is a fresh MARKET batch already admitted by the shared execution
        # engine's time/revision/cancellation/trading checks. Do not re-decide the
        # provider's market instruction from another quote read. Send the unchanged
        # side, volume, SL and TP to MT5 and let the broker be authoritative.
        return await self._base.place_market_order(**kwargs)

    place_market_order._fresh_market_reaches_broker = True  # type: ignore[attr-defined]
    Day28ZoneGuardTradeGateway.place_market_order = place_market_order


def _install_critical_order_persistence_fix() -> None:
    from app.paper_critical_execution import PaperCriticalExecutionService

    original = PaperCriticalExecutionService._record_critical_order
    if getattr(original, "_postgres_status_bind_fixed", False):
        return

    def record_critical_order(
        self: Any,
        item: Any,
        order_id: str,
        position_id: str | None,
    ) -> None:
        status = (
            "open"
            if position_id
            else "pending"
            if item.entry.order_type != "market"
            else "planned"
        )
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET broker_order_id=:order_id,
                        broker_position_id=COALESCE(:position_id, broker_position_id),
                        status=:status,
                        opened_at=CASE WHEN :is_open
                                       THEN COALESCE(opened_at, now())
                                       ELSE opened_at END,
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": item.local_id,
                    "order_id": order_id,
                    "position_id": position_id,
                    "status": status,
                    "is_open": status == "open",
                },
            )
            session.commit()

    record_critical_order._postgres_status_bind_fixed = True  # type: ignore[attr-defined]
    PaperCriticalExecutionService._record_critical_order = record_critical_order


def install_execution_path_reliability_overrides() -> None:
    global _installed
    if _installed:
        return
    _install_fresh_market_submission_policy()
    _install_critical_order_persistence_fix()
    _installed = True


__all__ = ["install_execution_path_reliability_overrides"]
