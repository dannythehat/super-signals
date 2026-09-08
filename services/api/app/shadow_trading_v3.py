"""Execution-price corrections for the Provider Lab fair shadow runtime.

BUY actions enter on ask and exit on bid; SELL actions enter on bid and exit on ask.
This makes spread a real cost instead of an artificial advantage, which is particularly
important for scalpers. The websocket listener also uses the current MetaApi callback
signature and excludes same-observation entry/stop gaps where event order is unknowable.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from app.provider_fairness import score_eligibility
from app.shadow_trading_v2 import (
    MetaApi,
    ShadowTradeManager as _BaseShadowTradeManager,
    ShadowTradeService,
    SynchronizationListener,
    _decimal,
)

logger = logging.getLogger(__name__)


def _pit_safe_score_eligibility(row: Any, *, quote_mode: str) -> tuple[bool, str | None]:
    """Apply market-resolution scoring only to PIT-resolved provider profiles.

    Legacy rows are intentionally unresolvable at their historical signal timestamp.
    They may continue through shadow lifecycle research, but they can never regain score
    eligibility. Preserve an existing exclusion reason when one is already recorded.
    """
    pit_status = str(row["provider_profile_pit_status"] or "").strip()
    if pit_status != "resolved":
        existing_reason = str(row["score_exclusion_reason"] or "").strip()
        return False, existing_reason or "legacy_profile_unresolvable"
    return score_eligibility(
        style=str(row["provider_style"]),
        quote_mode=quote_mode,
    )


class _FairGoldQuoteListener(SynchronizationListener):
    def __init__(self, manager: "ShadowTradeManager") -> None:
        self._manager = manager

    async def on_symbol_price_updated(self, instance_index: int, price: Any) -> None:
        del instance_index
        if str(price.get("symbol") or "").upper() != "XAUUSD":
            return
        await self._manager._handle_stream_price(
            bid=_decimal(price.get("bid")),
            ask=_decimal(price.get("ask")),
            quote_mode="stream_quote",
        )

    async def on_ticks_updated(
        self,
        instance_index: int,
        ticks: list[Any],
        equity: float | None = None,
        margin: float | None = None,
        free_margin: float | None = None,
        margin_level: float | None = None,
        account_currency_exchange_rate: float | None = None,
    ) -> None:
        del instance_index, equity, margin, free_margin, margin_level, account_currency_exchange_rate
        for tick in ticks:
            if str(tick.get("symbol") or "").upper() != "XAUUSD":
                continue
            await self._manager._handle_stream_price(
                bid=_decimal(tick.get("bid")),
                ask=_decimal(tick.get("ask")),
                quote_mode="stream_tick",
            )


class ShadowTradeManager(_BaseShadowTradeManager):
    """Fair shadow evaluator using executable bid/ask sides and tick-quality gates."""

    async def _run_stream(self) -> None:
        if MetaApi is None:
            logger.warning("Provider Lab tick stream unavailable: MetaApi SDK not installed")
            return
        while not self._stopping.is_set():
            try:
                account = self._broker_account()
                if account is None:
                    await asyncio.sleep(15)
                    continue
                account_id, ciphertext = account
                token = self._cipher.decrypt(ciphertext)
                api = MetaApi(token)
                account_object = await api.metatrader_account_api.get_account(account_id)
                connection = account_object.get_streaming_connection()
                listener = _FairGoldQuoteListener(self)
                connection.add_synchronization_listener(listener)
                self._stream_api = api
                self._stream_connection = connection
                await connection.connect()
                await connection.wait_synchronized()
                await connection.subscribe_to_market_data(
                    "XAUUSD",
                    [
                        {"type": "quotes", "intervalInMilliseconds": 1000},
                        {"type": "ticks"},
                    ],
                )
                logger.info("Provider Lab fair XAUUSD tick/quote stream connected")
                while not self._stopping.is_set():
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Provider Lab stream unavailable code=%s", type(exc).__name__)
            finally:
                await self._close_stream_resources()
            if not self._stopping.is_set():
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=15)
                except TimeoutError:
                    pass

    @staticmethod
    def _target_already_passed(*, side: str, entry_price: Decimal, targets: list[Decimal]) -> bool:
        if not targets:
            return False
        first = min(targets) if side == "BUY" else max(targets)
        return entry_price >= first if side == "BUY" else entry_price <= first

    def _cancel_unscored_pending(
        self,
        session,
        row: Any,
        *,
        entry_price: Decimal,
        quote_mode: str,
        reason: str,
    ) -> bool:
        session.execute(
            text(
                """
                UPDATE shadow_trades SET status='missed',quote_mode=:mode,score_eligible=false,
                    score_exclusion_reason=:reason,close_reason=:reason,last_price=:price,
                    closed_at=now(),updated_at=now() WHERE id=:id
                """
            ),
            {"id": row["id"], "mode": quote_mode, "reason": reason, "price": entry_price},
        )
        session.execute(
            text(
                """
                UPDATE shadow_trade_legs SET status='cancelled',remaining_fraction=0,
                    exit_reason=:reason,closed_at=now(),updated_at=now()
                WHERE shadow_trade_id=:id AND status='pending'
                """
            ),
            {"id": row["id"], "reason": reason},
        )
        return True

    def _evaluate_row(
        self,
        session,
        row: Any,
        *,
        bid: Decimal,
        ask: Decimal,
        quote_mode: str,
    ) -> bool:
        side = str(row["side"])
        entry_executable = ask if side == "BUY" else bid
        exit_executable = bid if side == "BUY" else ask
        spread = abs(ask - bid)
        status = str(row["status"])
        low = _decimal(row["entry_low"])
        high = _decimal(row["entry_high"])
        stop = _decimal(row["current_stop"])
        if low is None or high is None or stop is None:
            return False

        if status == "pending":
            previous = _decimal(row["last_price"])
            entry_order_type = str(row["entry_order_type"] or "zone")
            immediate_market = entry_order_type == "market"
            triggered = immediate_market or self._crossed_zone(
                previous,
                entry_executable,
                min(low, high),
                max(low, high),
            )
            passed_stop = (
                side == "BUY" and entry_executable <= stop
            ) or (
                side == "SELL" and entry_executable >= stop
            )
            targets = [
                value
                for value in (_decimal(item) for item in (row["take_profits"] or []))
                if value is not None
            ]

            if immediate_market and passed_stop:
                return self._cancel_unscored_pending(
                    session,
                    row,
                    entry_price=entry_executable,
                    quote_mode=quote_mode,
                    reason="market_signal_arrived_beyond_stop",
                )
            if immediate_market and self._target_already_passed(
                side=side,
                entry_price=entry_executable,
                targets=targets,
            ):
                return self._cancel_unscored_pending(
                    session,
                    row,
                    entry_price=entry_executable,
                    quote_mode=quote_mode,
                    reason="market_signal_arrived_after_first_target",
                )

            # If one observation spans both entry and stop we cannot prove event order,
            # even with a tick gap. Exclude rather than manufacture a win/loss.
            if triggered and passed_stop and not immediate_market:
                return self._cancel_unscored_pending(
                    session,
                    row,
                    entry_price=entry_executable,
                    quote_mode=quote_mode,
                    reason="entry_stop_sequence_ambiguous_same_observation",
                )
            if passed_stop and not triggered:
                return self._cancel_unscored_pending(
                    session,
                    row,
                    entry_price=entry_executable,
                    quote_mode=quote_mode,
                    reason="price_passed_stop_before_entry",
                )
            if not triggered:
                session.execute(
                    text(
                        "UPDATE shadow_trades SET last_price=:price,quote_mode=:mode,updated_at=now() WHERE id=:id"
                    ),
                    {"id": row["id"], "price": entry_executable, "mode": quote_mode},
                )
                return True

            # Use the actual observed executable side, not the provider's prettier
            # advertised entry, so spread/gaps/slippage cannot be hidden.
            entry = entry_executable
            eligible, reason = _pit_safe_score_eligibility(
                row,
                quote_mode=quote_mode,
            )
            posted_at = row["signal_posted_at"]
            delay_ms: int | None = None
            if isinstance(posted_at, datetime):
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=UTC)
                delay_ms = max(
                    0,
                    int((datetime.now(UTC) - posted_at.astimezone(UTC)).total_seconds() * 1000),
                )
            session.execute(
                text(
                    """
                    UPDATE shadow_trades SET status='open',entry_price=:entry,last_price=:exit_price,
                        opened_at=now(),max_price=:exit_price,min_price=:exit_price,quote_mode=:mode,
                        score_eligible=:eligible,score_exclusion_reason=:reason,
                        entry_delay_ms=:delay_ms,entry_spread=:spread,updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": row["id"],
                    "entry": entry,
                    "exit_price": exit_executable,
                    "mode": quote_mode,
                    "eligible": eligible,
                    "reason": reason,
                    "delay_ms": delay_ms,
                    "spread": spread,
                },
            )
            session.execute(
                text(
                    """
                    UPDATE shadow_trade_legs SET status='open',opened_at=now(),updated_at=now()
                    WHERE shadow_trade_id=:id AND status='pending'
                    """
                ),
                {"id": row["id"]},
            )
            return True

        entry = _decimal(row["entry_price"])
        initial_stop = _decimal(row["initial_stop"])
        if entry is None or initial_stop is None or entry == initial_stop:
            return False

        session.execute(
            text(
                """
                UPDATE shadow_trades SET last_price=:price,quote_mode=:mode,
                    max_price=GREATEST(COALESCE(max_price,:price),:price),
                    min_price=LEAST(COALESCE(min_price,:price),:price),updated_at=now()
                WHERE id=:id
                """
            ),
            {"id": row["id"], "price": exit_executable, "mode": quote_mode},
        )

        legs = list(
            session.execute(
                text(
                    """
                    SELECT * FROM shadow_trade_legs
                    WHERE shadow_trade_id=:trade_id AND status='open'
                    ORDER BY tp_index
                    """
                ),
                {"trade_id": row["id"]},
            ).mappings()
        )
        changed = False
        for leg in legs:
            if bool(leg["is_runner"]):
                continue
            target = _decimal(leg["target_price"])
            if target is None:
                continue
            reached = exit_executable >= target if side == "BUY" else exit_executable <= target
            if not reached:
                continue
            if str(row["provider_style"]) == "scalper" and quote_mode != "stream_tick":
                self._mark_ineligible(session, row["id"], "scalper_exit_requires_tick_resolution")
            changed = ShadowTradeService._realize_leg_fraction(
                session,
                leg=leg,
                entry=entry,
                initial_stop=initial_stop,
                side=side,
                exit_price=target,
                fraction=Decimal("1"),
                final_reason="target",
            ) or changed

        stopped = exit_executable <= stop if side == "BUY" else exit_executable >= stop
        if stopped:
            if str(row["provider_style"]) == "scalper" and quote_mode != "stream_tick":
                self._mark_ineligible(session, row["id"], "scalper_stop_requires_tick_resolution")
            remaining = list(
                session.execute(
                    text(
                        "SELECT * FROM shadow_trade_legs WHERE shadow_trade_id=:id AND status='open' ORDER BY tp_index"
                    ),
                    {"id": row["id"]},
                ).mappings()
            )
            for leg in remaining:
                changed = ShadowTradeService._realize_leg_fraction(
                    session,
                    leg=leg,
                    entry=entry,
                    initial_stop=initial_stop,
                    side=side,
                    exit_price=stop,
                    fraction=Decimal("1"),
                    final_reason="shadow_stop",
                ) or changed

        if changed or stopped:
            ShadowTradeService._aggregate_trade(
                session,
                row["id"],
                preferred_reason=("shadow_stop" if stopped else None),
            )
        return True


__all__ = ["ShadowTradeManager"]
