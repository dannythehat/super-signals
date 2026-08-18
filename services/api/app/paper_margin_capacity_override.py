"""Owner-DEMO capacity fallback for proven layered provider setups.

Paper testing exists to observe provider trades. A proven provider layer grid must not be
turned into an all-or-nothing bet merely because the small Owner DEMO account cannot fund
every declared layer at the broker minimum volume.

This override is deliberately narrow:
* Owner DEMO / PaperFreshStartExecutionService only;
* it never bypasses broker free margin;
* it never reduces the selected risk on an admitted provider section;
* it never changes an entry price, side, SL or TP;
* it applies only when there are more declared entry layers than TP/runner targets;
* every TP/runner target must still be represented at least once;
* provider layers are admitted in their literal order until the fresh free-margin budget
  is exhausted; omitted deeper layers are recorded in audit evidence.

Ordinary single-entry/multi-TP signals retain their existing all-or-nothing margin gate.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_execution_day26 import Day26ExecutionError
from app.paper_fresh_start_execution import PaperFreshStartExecutionService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PaperMarginCapacityPlan:
    requested_entry_layers: int
    admitted_entry_layers: int
    target_count: int
    free_margin: Decimal
    requested_margin: Decimal
    admitted_margin: Decimal


_capacity_plan: ContextVar[PaperMarginCapacityPlan | None] = ContextVar(
    "super_signals_paper_margin_capacity_plan",
    default=None,
)
_installed = False


def _max_affordable_prefix(
    required_by_entry: tuple[Decimal, ...],
    *,
    free_margin: Decimal,
) -> tuple[int, Decimal]:
    """Return the largest literal-order prefix whose summed margin fits."""
    admitted = 0
    used = Decimal("0")
    for required in required_by_entry:
        if required < 0:
            raise ValueError("margin_requirement_invalid")
        candidate = used + required
        if candidate > free_margin:
            break
        used = candidate
        admitted += 1
    return admitted, used


async def _capacity_margin_preflight(
    self: Any,
    *,
    token: str,
    account_id: str,
    region: str,
    symbol: str,
    side: str,
    free_margin: Decimal,
    entries: tuple[Any, ...],
    sizings: dict[int, Any],
) -> None:
    """Admit the largest TP-complete provider-layer prefix that actually fits."""
    _capacity_plan.set(None)
    if not sizings:
        raise Day26ExecutionError("position_count_invalid")

    target_count = int(next(iter(sizings.values())).position_count)
    if target_count <= 0:
        raise Day26ExecutionError("position_count_invalid")

    # Capacity reduction is only meaningful for a proven layered grid with surplus
    # entry layers. Signals whose broker positions exist only to represent TP targets
    # keep the established atomic/all-or-nothing behaviour.
    if len(entries) <= target_count:
        return await _original_margin_preflight(
            self,
            token=token,
            account_id=account_id,
            region=region,
            symbol=symbol,
            side=side,
            free_margin=free_margin,
            entries=entries,
            sizings=sizings,
        )

    required_by_entry: list[Decimal] = []
    for entry in entries:
        sizing = sizings[entry.entry_index]
        try:
            required = await self._margin_gateway.calculate_margin(
                token=token,
                account_id=account_id,
                region=region,
                symbol=symbol,
                side=side,
                volume=float(sizing.volume),
                open_price=float(entry.price),
            )
        except MetaApiGatewayError:
            # Match the established paper policy when advisory margin calculation is
            # unavailable: do not invent a capacity number; let the broker decide.
            return
        parsed = Decimal(str(required))
        if not parsed.is_finite() or parsed < 0:
            return
        required_by_entry.append(parsed)

    requested_margin = sum(required_by_entry, Decimal("0"))
    if requested_margin <= free_margin:
        return

    admitted_count, admitted_margin = _max_affordable_prefix(
        tuple(required_by_entry),
        free_margin=free_margin,
    )
    if admitted_count < target_count:
        # We refuse to pretend a provider's multi-target trade was followed when the
        # account cannot even represent every declared TP/runner target once.
        raise Day26ExecutionError("insufficient_funds")

    plan = PaperMarginCapacityPlan(
        requested_entry_layers=len(entries),
        admitted_entry_layers=admitted_count,
        target_count=target_count,
        free_margin=free_margin,
        requested_margin=requested_margin,
        admitted_margin=admitted_margin,
    )
    _capacity_plan.set(plan)
    logger.warning(
        "Owner DEMO margin capacity reduced provider layer grid requested=%d admitted=%d "
        "targets=%d requested_margin=%s admitted_margin=%s free_margin=%s",
        plan.requested_entry_layers,
        plan.admitted_entry_layers,
        plan.target_count,
        plan.requested_margin,
        plan.admitted_margin,
        plan.free_margin,
    )


def _capacity_create_layered_plans(
    self: Any,
    *,
    owner_user_id: Any,
    signal: Any,
    entries: tuple[Any, ...],
    sizings: dict[int, Any],
    market_entry: Decimal,
):
    plan = _capacity_plan.get()
    selected_entries = entries
    if plan is not None and plan.admitted_entry_layers < len(entries):
        selected_entries = entries[: plan.admitted_entry_layers]

    created = _original_create_layered_plans(
        self,
        owner_user_id=owner_user_id,
        signal=signal,
        entries=selected_entries,
        sizings=sizings,
        market_entry=market_entry,
    )

    if plan is not None and plan.admitted_entry_layers < plan.requested_entry_layers:
        omitted_prices = [
            str(item.price)
            for item in entries[plan.admitted_entry_layers :]
        ]
        payload = {
            "paper_demo_only": True,
            "provider_semantics_preserved": True,
            "risk_reduced": False,
            "trade_action_created": False,
            "requested_entry_layers": plan.requested_entry_layers,
            "admitted_entry_layers": plan.admitted_entry_layers,
            "omitted_entry_layers": plan.requested_entry_layers - plan.admitted_entry_layers,
            "target_count": plan.target_count,
            "free_margin": str(plan.free_margin),
            "requested_margin": str(plan.requested_margin),
            "admitted_margin": str(plan.admitted_margin),
            "omitted_entry_prices": omitted_prices,
        }
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        actor_user_id, event_type, entity_type, entity_id, payload
                    ) VALUES (
                        :actor, 'mt5.paper_margin_capacity_reduced', 'signal', :signal_id,
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "actor": owner_user_id,
                    "signal_id": signal.signal_id,
                    "payload": json.dumps(payload, separators=(",", ":")),
                },
            )
            session.commit()
    return created


async def _capacity_execute_owner_demo_signal(self: Any, **kwargs: Any):
    token = _capacity_plan.set(None)
    try:
        return await _original_execute_owner_demo_signal(self, **kwargs)
    finally:
        _capacity_plan.reset(token)


def install_paper_margin_capacity_override() -> None:
    global _installed, _original_margin_preflight, _original_create_layered_plans
    global _original_execute_owner_demo_signal
    if _installed:
        return

    cls = PaperFreshStartExecutionService
    _original_margin_preflight = cls._margin_preflight
    _original_create_layered_plans = cls._create_layered_plans
    _original_execute_owner_demo_signal = cls.execute_owner_demo_signal

    cls._margin_preflight = _capacity_margin_preflight
    cls._create_layered_plans = _capacity_create_layered_plans
    cls.execute_owner_demo_signal = _capacity_execute_owner_demo_signal
    _installed = True


# Assigned during installation; annotations keep static tooling clear.
_original_margin_preflight: Any
_original_create_layered_plans: Any
_original_execute_owner_demo_signal: Any


__all__ = [
    "PaperMarginCapacityPlan",
    "_max_affordable_prefix",
    "install_paper_margin_capacity_override",
]
