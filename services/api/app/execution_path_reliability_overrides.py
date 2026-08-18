"""Paper-execution reliability corrections discovered during live provider testing.

Two provider-dependent failures were observed in production paper testing:

* Critical/layered execution could successfully submit a broker order and then crash
  while persisting the order because PostgreSQL inferred two incompatible types for
  the same ``:status`` bind parameter inside an UPDATE/CASE expression.
* A market-zone signal was admitted at a fresh executable price, but the Day 28
  gateway re-sampled price before every TP tranche. A tiny move during the few
  hundred milliseconds needed to submit sibling TP positions could therefore abort
  the remainder and roll back an otherwise valid signal.

The policy here is intentionally narrow:
* broker order placement is still atomic/fail-closed;
* a zone is freshly checked immediately before the FIRST broker submission;
* once that batch is admitted, sibling TP tranches of that same signal are submitted
  without re-deciding the provider entry zone between legs;
* a broker/order failure still triggers the existing compensating rollback;
* exact-entry and pending-order behaviour is unchanged.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from sqlalchemy import text

_batch_zone_admitted: ContextVar[bool] = ContextVar(
    "super_signals_batch_zone_admitted", default=False
)
_installed = False


def _install_zone_batch_admission() -> None:
    from app.day28_zone_guard import Day28ZoneGuardTradeGateway

    original_set_zone = Day28ZoneGuardTradeGateway.set_zone
    original_reset_zone = Day28ZoneGuardTradeGateway.reset_zone
    original_place = Day28ZoneGuardTradeGateway.place_market_order

    if getattr(original_place, "_single_batch_zone_admission", False):
        return

    def set_zone(self: Any, low: Any, high: Any):
        zone_token = original_set_zone(self, low, high)
        admitted_token = _batch_zone_admitted.set(False)
        return zone_token, admitted_token

    def reset_zone(self: Any, token: Any) -> None:
        if isinstance(token, tuple) and len(token) == 2:
            zone_token, admitted_token = token
            try:
                original_reset_zone(self, zone_token)
            finally:
                _batch_zone_admitted.reset(admitted_token)
            return
        original_reset_zone(self, token)

    async def place_market_order(self: Any, **kwargs: Any):
        # Exact-entry execution never sets a zone and therefore continues through the
        # original gateway unchanged. For a zone batch, the original gateway performs
        # the fresh broker-price admission check on the first leg only.
        zone = self._zone.get()
        if zone is None or not _batch_zone_admitted.get():
            result = await original_place(self, **kwargs)
            if zone is not None:
                _batch_zone_admitted.set(True)
            return result

        # The signal has already been admitted immediately before its first broker
        # submission. Sibling TP tranches are one atomic execution batch, not new
        # trading decisions, so do not re-evaluate the provider zone between them.
        return await self._base.place_market_order(**kwargs)

    place_market_order._single_batch_zone_admission = True  # type: ignore[attr-defined]
    Day28ZoneGuardTradeGateway.set_zone = set_zone
    Day28ZoneGuardTradeGateway.reset_zone = reset_zone
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
    _install_zone_batch_admission()
    _install_critical_order_persistence_fix()
    _installed = True


__all__ = ["install_execution_path_reliability_overrides"]
