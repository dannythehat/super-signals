"""Owner paper market execution policy.

Fresh market signals are executed at the current broker executable price. Provider entry
zones remain evidence/context and are not a second broker-placement veto after a fresh
market instruction has already passed the canonical signal gate. Explicit pending and
multi-entry structures keep their literal broker-side semantics.
"""

from __future__ import annotations

from typing import Any

from app.critical_entry_policy import parse_critical_entries
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.paper_critical_execution import PaperCriticalExecutionService


def install_paper_market_execution_policy() -> None:
    """Bypass provider-zone submission checks for ordinary fresh Owner DEMO market trades."""
    from app.paper_execution_priority import PaperExecutionPriorityService

    cls = PaperExecutionPriorityService
    current = cls.execute_owner_demo_signal
    if getattr(current, "_paper_market_zone_veto_removed", False):
        return

    async def execute_owner_demo_signal(
        self: Any,
        *,
        owner_user_id,
        signal_id,
        risk_percent,
        double_lot_approved: bool,
    ):
        critical = self._load_critical_signal(signal_id)
        try:
            entries = parse_critical_entries(
                critical.original_text,
                side=critical.base.side,
                entry_low=critical.base.entry_low,
                entry_high=critical.base.entry_high,
            )
        except ValueError as exc:
            raise Day26ExecutionError(str(exc)) from exc

        # Pending and genuine multi-entry structures still use the dedicated critical
        # execution path because their literal broker-side prices are part of provider
        # intent. Ordinary exact/zone market signals go directly to the atomic executor;
        # dynamic method dispatch still applies the Owner paper freshness, live-price,
        # SL/TP geometry, risk sizing, margin and mapping rules on this instance.
        is_critical = critical.broad_order_type == "pending" or len(entries) > 1
        if is_critical:
            return await PaperCriticalExecutionService.execute_owner_demo_signal(
                self,
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                risk_percent=risk_percent,
                double_lot_approved=double_lot_approved,
            )

        return await AtomicDay26Mt5ExecutionService.execute_owner_demo_signal(
            self,
            owner_user_id=owner_user_id,
            signal_id=signal_id,
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )

    execute_owner_demo_signal._paper_market_zone_veto_removed = True  # type: ignore[attr-defined]
    cls.execute_owner_demo_signal = execute_owner_demo_signal


__all__ = ["install_paper_market_execution_policy"]
