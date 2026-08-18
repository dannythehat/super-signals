"""Allow only the exact bare-Gold-NOW execution profile through production loaders.

The canonical ledger intentionally keeps provider entry/SL/TP absent for a standalone
``BUY/SELL GOLD NOW`` message.  The paper executor derives its temporary 50-pip TP and
100-pip SL from the fresh broker fill later in ``_resolve_entry``.  Legacy Day 28 /
critical loaders historically rejected the empty provider TP list before that profile
could run.  This module bridges that single profile without relaxing validation for any
other incomplete signal.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.bare_gold_now_override import bare_now_side
from app.mt5_execution_day26 import Day26ExecutionError, _SignalInput
from app.paper_critical_execution import _CriticalSignal
from app.paper_execution_priority import PaperExecutionPriorityService


def _bare_critical_signal(service: Any, signal_id: Any) -> _CriticalSignal | None:
    with service._session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT symbol, side, order_type, entry_low, entry_high,
                       stop_loss, take_profits, has_open_runner, parser_status,
                       risk_multiplier, source_revision_index, source_posted_at,
                       original_text
                FROM signals
                WHERE id=:signal_id
                LIMIT 1
                """
            ),
            {"signal_id": signal_id},
        ).mappings().first()

    if row is None:
        return None

    raw_text = str(row["original_text"] or "")
    literal_side = bare_now_side(raw_text)
    if literal_side is None:
        return None

    symbol = str(row["symbol"] or "").strip().upper()
    side = str(row["side"] or "").strip().upper()
    order_type = str(row["order_type"] or "").strip().lower()
    targets = row["take_profits"]
    if targets is None:
        targets = []

    # This exception is intentionally exact.  If any provider-defined execution
    # parameter exists, this is not the bare-NOW profile and normal validation owns it.
    if (
        str(row["parser_status"] or "") != "accepted"
        or symbol != "XAUUSD"
        or side != literal_side
        or order_type != "market"
        or row["entry_low"] is not None
        or row["entry_high"] is not None
        or row["stop_loss"] is not None
        or not isinstance(targets, (list, tuple))
        or len(targets) != 0
        or bool(row["has_open_runner"])
    ):
        raise Day26ExecutionError("bare_gold_now_profile_invalid")

    posted_at = row["source_posted_at"]
    if not isinstance(posted_at, datetime):
        raise Day26ExecutionError("signal_posted_at_invalid")

    risk_multiplier = service._required_decimal(
        row["risk_multiplier"], "signal_risk_multiplier_invalid"
    )
    if risk_multiplier != Decimal("1"):
        raise Day26ExecutionError("bare_gold_now_profile_invalid")

    base = _SignalInput(
        signal_id=signal_id,
        symbol="XAUUSD",
        side=side,
        entry_low=Decimal("0"),
        entry_high=Decimal("0"),
        stop_loss=Decimal("0"),
        take_profits=(),
        has_open_runner=False,
        signal_requests_double_lot=False,
        source_revision_index=int(row["source_revision_index"]),
        source_posted_at=posted_at,
    )
    return _CriticalSignal(
        base=base,
        original_text=raw_text,
        broad_order_type="market",
    )


def install_bare_gold_now_loader_fix() -> None:
    cls = PaperExecutionPriorityService

    original_critical = cls._load_critical_signal
    if not getattr(original_critical, "_bare_gold_now_loader_fix", False):

        def load_critical_signal(self: Any, signal_id: Any):
            try:
                return original_critical(self, signal_id)
            except Day26ExecutionError as exc:
                if exc.code != "signal_take_profits_invalid":
                    raise
                special = _bare_critical_signal(self, signal_id)
                if special is None:
                    raise
                return special

        load_critical_signal._bare_gold_now_loader_fix = True  # type: ignore[attr-defined]
        cls._load_critical_signal = load_critical_signal

    original_inputs = cls._load_inputs
    if not getattr(original_inputs, "_bare_gold_now_loader_fix", False):

        def load_inputs(self: Any, owner_user_id: Any, signal_id: Any):
            try:
                return original_inputs(self, owner_user_id, signal_id)
            except Day26ExecutionError as exc:
                if exc.code != "signal_take_profits_invalid":
                    raise
                special = _bare_critical_signal(self, signal_id)
                if special is None:
                    raise
                # Reuse the critical paper account loader so cancellation, duplicate
                # execution, DEMO-only and connected-account checks remain intact.
                account = self._load_demo_account(owner_user_id, signal_id)
                return special.base, account

        load_inputs._bare_gold_now_loader_fix = True  # type: ignore[attr-defined]
        cls._load_inputs = load_inputs


__all__ = ["install_bare_gold_now_loader_fix"]
