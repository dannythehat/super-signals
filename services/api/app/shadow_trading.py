"""Isolated virtual execution for candidate Telegram providers.

Shadow trades never create rows in ``positions`` and never call a trade gateway. They
use the owner demo account only as a read-only XAUUSD price feed. Provider Lab evaluates
providers on one fixed benchmark: $1,000 starting balance and $10 risk for every TP leg.
That means 1R on one leg equals $10, or 1% of the benchmark account.

Research evaluation follows provider instructions only. House rules such as automatically
moving to breakeven after TP1/TP2 are deliberately NOT imposed here because doing so would
measure Super Signals' management rather than the provider's actual strategy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

logger = logging.getLogger(__name__)

_BENCHMARK_START_BALANCE = Decimal("1000")
_BENCHMARK_RISK_PER_LEG = Decimal("10")
_CENTS = Decimal("0.01")


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _leg_r(*, entry: Decimal, exit_price: Decimal, initial_stop: Decimal, side: str) -> Decimal:
    risk_distance = abs(entry - initial_stop)
    if risk_distance == 0:
        return Decimal("0")
    direction = Decimal("1") if side == "BUY" else Decimal("-1")
    return ((exit_price - entry) * direction) / risk_distance


def _benchmark_pnl_usd(quality_r: Decimal) -> Decimal:
    return (quality_r * _BENCHMARK_RISK_PER_LEG).quantize(_CENTS, rounding=ROUND_HALF_UP)


class ShadowTradeService:
    """Persist canonical signals and provider management in a broker-proof shadow ledger."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record_signal(self, signal_id: UUID) -> bool:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT s.id AS signal_id,m.id AS message_id,m.source_id,m.posted_at,
                           src.updated_at AS shadow_started_at,s.symbol,s.side,s.order_type,
                           s.entry_low,s.entry_high,s.stop_loss,p.take_profits
                    FROM signals s
                    JOIN messages m ON m.id=s.source_message_id
                    JOIN sources src ON src.id=m.source_id
                    LEFT JOIN message_parses p
                      ON p.message_id=m.id AND p.revision_index=s.source_revision_index
                    WHERE s.id=:signal_id AND s.parser_status='accepted'
                      AND src.status='shadow' AND m.deleted_at IS NULL
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if row is None or row["posted_at"] < row["shadow_started_at"]:
                return False
            targets = [
                str(value)
                for value in (row["take_profits"] or [])
                if _decimal(value) is not None
            ]
            if not targets or row["stop_loss"] is None or row["side"] not in {"BUY", "SELL"}:
                return False
            session.execute(
                text(
                    """
                    INSERT INTO shadow_trades(
                        source_id,signal_id,message_id,symbol,side,order_type,entry_low,
                        entry_high,initial_stop,current_stop,take_profits,status
                    ) VALUES (
                        :source_id,:signal_id,:message_id,:symbol,:side,:order_type,:entry_low,
                        :entry_high,:stop_loss,:stop_loss,CAST(:take_profits AS jsonb),'pending'
                    )
                    ON CONFLICT (signal_id) DO NOTHING
                    """
                ),
                {
                    "source_id": row["source_id"],
                    "signal_id": signal_id,
                    "message_id": row["message_id"],
                    "symbol": str(row["symbol"] or "XAUUSD").upper(),
                    "side": row["side"],
                    "order_type": row["order_type"],
                    "entry_low": row["entry_low"],
                    "entry_high": row["entry_high"],
                    "stop_loss": row["stop_loss"],
                    "take_profits": json.dumps(targets),
                },
            )
            session.commit()
            return True

    def record_management(self, lifecycle_event_id: UUID) -> bool:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT e.signal_id,e.aggregate_result,t.id,t.status,t.entry_price,t.last_price,
                           t.side,t.take_profits,t.hit_targets,t.initial_stop,t.current_stop,
                           t.quality_r_multiple,t.realized_percent
                    FROM signal_lifecycle_events e
                    JOIN shadow_trades t ON t.signal_id=e.signal_id
                    WHERE e.id=:event_id
                    LIMIT 1
                    """
                ),
                {"event_id": lifecycle_event_id},
            ).mappings().first()
            if row is None or row["status"] in {"closed", "cancelled", "missed"}:
                return False
            payload = dict(row["aggregate_result"] or {})
            revised = dict(payload.get("revised_instruction") or {})
            actions = list(revised.get("management_actions") or [])
            changed = False
            for action in actions:
                kind = str(action.get("type") or "").lower()
                target = str(action.get("target") or "all").lower()
                value = _decimal(action.get("value"))
                if kind in {"move_to_break_even", "breakeven"} and row["entry_price"] is not None:
                    session.execute(
                        text("UPDATE shadow_trades SET current_stop=entry_price,updated_at=now() WHERE id=:id"),
                        {"id": row["id"]},
                    )
                    changed = True
                elif kind in {"edit_stop_loss", "move_stop_loss"} and value is not None:
                    session.execute(
                        text("UPDATE shadow_trades SET current_stop=:value,updated_at=now() WHERE id=:id"),
                        {"id": row["id"], "value": value},
                    )
                    changed = True
                elif kind in {"cancel", "cancel_pending"} and row["status"] == "pending":
                    session.execute(
                        text("UPDATE shadow_trades SET status='cancelled',close_reason='provider_cancelled',closed_at=now(),updated_at=now() WHERE id=:id"),
                        {"id": row["id"]},
                    )
                    changed = True
                elif kind in {"close", "close_trade"}:
                    changed = self._apply_provider_close(session, row, target) or changed
            if changed:
                session.commit()
            return changed

    def _apply_provider_close(self, session: Session, row: Any, target: str) -> bool:
        if row["status"] == "pending":
            session.execute(
                text("UPDATE shadow_trades SET status='cancelled',close_reason='provider_closed_before_entry',closed_at=now(),updated_at=now() WHERE id=:id"),
                {"id": row["id"]},
            )
            return True
        entry = _decimal(row["entry_price"])
        price = _decimal(row["last_price"])
        initial_stop = _decimal(row["initial_stop"])
        if entry is None or price is None or initial_stop is None or entry == initial_stop:
            return False
        targets = list(row["take_profits"] or [])
        hit = {int(value) for value in (row["hit_targets"] or [])}
        digits = "".join(ch for ch in target if ch.isdigit())
        if digits:
            indexes = [int(digits)]
        else:
            indexes = [i for i in range(1, len(targets) + 1) if i not in hit]
        quality_r = _decimal(row["quality_r_multiple"]) or Decimal("0")
        for index in indexes:
            if index in hit or index < 1 or index > len(targets):
                continue
            quality_r += _leg_r(
                entry=entry,
                exit_price=price,
                initial_stop=initial_stop,
                side=str(row["side"]),
            )
            hit.add(index)
        closed = len(hit) >= len(targets) or not digits
        benchmark_pnl = _benchmark_pnl_usd(quality_r)
        session.execute(
            text(
                """
                UPDATE shadow_trades
                SET hit_targets=CAST(:hit AS jsonb),quality_r_multiple=:quality_r,
                    benchmark_pnl_usd=:benchmark_pnl,realized_percent=:quality_r,
                    status=CASE WHEN :closed THEN 'closed' ELSE status END,
                    closed_at=CASE WHEN :closed THEN now() ELSE closed_at END,
                    close_reason=CASE WHEN :closed THEN 'provider_close' ELSE close_reason END,
                    pnl_percent=CASE WHEN :closed THEN :quality_r ELSE pnl_percent END,
                    updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": row["id"],
                "hit": json.dumps(sorted(hit)),
                "quality_r": quality_r,
                "benchmark_pnl": benchmark_pnl,
                "closed": closed,
            },
        )
        return True


class ShadowTradeManager:
    """Evaluate isolated shadow trades from one shared read-only broker quote."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        gateway: MetaApiReadGateway,
        owner_user_id: UUID,
        poll_seconds: int = 15,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway
        self._owner_user_id = owner_user_id
        self._poll_seconds = max(5, int(poll_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="super-signals-shadow-trades")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Shadow evaluator failed safely")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def poll_once(self) -> int:
        rows = self._active_rows()
        if not rows:
            return 0
        account = self._broker_account()
        if account is None:
            return 0
        account_id, ciphertext = account
        try:
            token = self._cipher.decrypt(ciphertext)
            region = await self._gateway.resolve_account_region(token=token, account_id=account_id)
            quote = await self._gateway.read_symbol_price(
                token=token,
                account_id=account_id,
                region=region,
                symbol="XAUUSD",
            )
        except (BrokerCredentialDecryptionError, MetaApiGatewayError) as exc:
            logger.warning(
                "Shadow quote unavailable code=%s",
                getattr(exc, "code", type(exc).__name__),
            )
            return 0
        bid = _decimal(quote.get("bid"))
        ask = _decimal(quote.get("ask"))
        if bid is None or ask is None:
            return 0
        changed = 0
        with self._session_factory() as session:
            for row in rows:
                changed += int(self._evaluate_row(session, row, bid=bid, ask=ask))
            session.commit()
        return changed

    def _active_rows(self) -> list[Any]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        "SELECT * FROM shadow_trades "
                        "WHERE status IN ('pending','open') ORDER BY created_at ASC"
                    )
                ).mappings()
            )

    def _broker_account(self) -> tuple[str, bytes] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id,metaapi_token_ciphertext FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND account_environment='demo'
                      AND status='connected'
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"user_id": self._owner_user_id},
            ).mappings().first()
        return None if row is None else (
            str(row["metaapi_account_id"]),
            bytes(row["metaapi_token_ciphertext"]),
        )

    def _evaluate_row(self, session: Session, row: Any, *, bid: Decimal, ask: Decimal) -> bool:
        side = str(row["side"])
        price = ask if side == "BUY" else bid
        status = str(row["status"])
        low = _decimal(row["entry_low"])
        high = _decimal(row["entry_high"])
        stop = _decimal(row["current_stop"])
        if stop is None:
            return False

        if status == "pending":
            market = str(row["order_type"]) == "market"
            triggered = (
                market
                or (side == "BUY" and high is not None and ask <= high)
                or (side == "SELL" and low is not None and bid >= low)
            )
            passed_stop = (
                (side == "BUY" and ask <= stop)
                or (side == "SELL" and bid >= stop)
            )
            if passed_stop and not market:
                session.execute(
                    text(
                        "UPDATE shadow_trades SET status='missed',"
                        "close_reason='price_passed_stop_before_observation',"
                        "closed_at=now(),updated_at=now() WHERE id=:id"
                    ),
                    {"id": row["id"]},
                )
                return True
            if not triggered:
                session.execute(
                    text(
                        "UPDATE shadow_trades SET last_price=:price,updated_at=now() "
                        "WHERE id=:id"
                    ),
                    {"id": row["id"], "price": price},
                )
                return True
            entry = price if market else (high if side == "BUY" else low)
            session.execute(
                text(
                    "UPDATE shadow_trades SET status='open',entry_price=:entry,"
                    "last_price=:price,opened_at=now(),max_price=:price,min_price=:price,"
                    "updated_at=now() WHERE id=:id"
                ),
                {"id": row["id"], "entry": entry, "price": price},
            )
            return True

        entry = _decimal(row["entry_price"])
        initial_stop = _decimal(row["initial_stop"])
        if entry is None or initial_stop is None or entry == initial_stop:
            return False
        targets = [_decimal(value) for value in (row["take_profits"] or [])]
        hit = {int(value) for value in (row["hit_targets"] or [])}
        quality_r = _decimal(row.get("quality_r_multiple")) or Decimal("0")

        # Every TP leg carries exactly the same $10 benchmark risk. Reaching a target
        # therefore adds that leg's R multiple without TP1/TP2/runner weighting.
        for index, target in enumerate(targets, start=1):
            if target is None or index in hit:
                continue
            reached = price >= target if side == "BUY" else price <= target
            if reached:
                quality_r += _leg_r(
                    entry=entry,
                    exit_price=target,
                    initial_stop=initial_stop,
                    side=side,
                )
                hit.add(index)

        # Research follows the provider's current stop exactly. No automatic house
        # breakeven or TP2-locking rule is applied to shadow providers.
        stopped = price <= stop if side == "BUY" else price >= stop
        if stopped:
            for index in range(1, len(targets) + 1):
                if index in hit:
                    continue
                quality_r += _leg_r(
                    entry=entry,
                    exit_price=stop,
                    initial_stop=initial_stop,
                    side=side,
                )
                hit.add(index)

        all_hit = len(hit) >= len(targets)
        closed = stopped or all_hit
        reason = "shadow_stop" if stopped else ("all_targets_hit" if all_hit else None)
        benchmark_pnl = _benchmark_pnl_usd(quality_r)
        session.execute(
            text(
                """
                UPDATE shadow_trades
                SET last_price=:price,
                    max_price=GREATEST(COALESCE(max_price,:price),:price),
                    min_price=LEAST(COALESCE(min_price,:price),:price),
                    hit_targets=CAST(:hit AS jsonb),
                    quality_r_multiple=:quality_r,
                    benchmark_pnl_usd=:benchmark_pnl,
                    realized_percent=:quality_r,
                    status=CASE WHEN :closed THEN 'closed' ELSE status END,
                    closed_at=CASE WHEN :closed THEN now() ELSE closed_at END,
                    pnl_percent=CASE WHEN :closed THEN :quality_r ELSE pnl_percent END,
                    close_reason=COALESCE(:reason,close_reason),
                    updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": row["id"],
                "price": price,
                "hit": json.dumps(sorted(hit)),
                "quality_r": quality_r,
                "benchmark_pnl": benchmark_pnl,
                "closed": closed,
                "reason": reason,
            },
        )
        return True
