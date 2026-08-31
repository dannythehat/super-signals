"""Provider-faithful, fixed-dollar shadow execution for Provider Lab.

Research providers never call broker mutation APIs. Every explicit entry action is tracked
independently and every TP/runner leg carries the same fixed $10 benchmark risk from a
common $1,000 reference balance. Provider management is followed literally. A shared
MetaApi websocket stream supplies high-resolution XAUUSD ticks for scalpers, while the
existing REST quote reader remains a fail-safe fallback. Low-resolution scalp outcomes
are retained for audit but excluded from provider scoring.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.critical_entry_policy import CriticalEntry, parse_critical_entries
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher
from app.provider_fairness import (
    BENCHMARK_MODEL,
    BENCHMARK_RISK_PER_LEG_USD,
    BENCHMARK_START_BALANCE_USD,
    score_eligibility,
    session_bucket,
)

try:  # installed in production; keeping import-safe behaviour helps focused unit tests
    from metaapi_cloud_sdk import MetaApi, SynchronizationListener
except ImportError:  # pragma: no cover - production image installs the SDK
    MetaApi = None  # type: ignore[assignment]

    class SynchronizationListener:  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")
_EPSILON = Decimal("0.000001")
_ENTRY_INDEX = re.compile(r"(?:entry[_\s-]*)(\d+)", re.IGNORECASE)
_FIRST_N = re.compile(r"first_(\d+)_layers?", re.IGNORECASE)
_WORST_N = re.compile(r"worst_(\d+)_layers?", re.IGNORECASE)
_ENTRY_PRICE = re.compile(r"entry_price_([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_TP_INDEX = re.compile(r"tp[_\s-]?(\d+)", re.IGNORECASE)


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
    return (quality_r * BENCHMARK_RISK_PER_LEG_USD).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _broad_order_type(entry_order_type: str) -> str:
    return "market" if entry_order_type == "market" else "pending"


def _is_terminal(status: str) -> bool:
    return status in {"closed", "cancelled", "missed"}


@dataclass(frozen=True, slots=True)
class _ResearchEntry:
    entry_index: int
    order_type: str
    low: Decimal
    high: Decimal


class ShadowTradeService:
    """Persist canonical shadow signals and provider management without broker mutation."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _research_entries(row: Any) -> tuple[_ResearchEntry, ...]:
        low = _decimal(row["entry_low"])
        high = _decimal(row["entry_high"])
        if low is None or high is None:
            return ()
        try:
            parsed = parse_critical_entries(
                str(row["original_text"] or ""),
                side=str(row["side"]),
                entry_low=low,
                entry_high=high,
            )
        except ValueError:
            parsed = ()

        if parsed:
            return tuple(
                _ResearchEntry(
                    entry_index=item.entry_index,
                    order_type=item.order_type,
                    low=item.price,
                    high=item.price,
                )
                for item in parsed
            )

        # A literal zone is one provider action unless the deterministic layer parser
        # proved multiple independent entries. It activates only when price enters or
        # crosses the provider's stated zone.
        return (
            _ResearchEntry(
                entry_index=1,
                order_type=("market" if low == high and str(row["order_type"]) == "market" else "zone"),
                low=min(low, high),
                high=max(low, high),
            ),
        )

    def record_signal(self, signal_id: UUID) -> bool:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT s.id AS signal_id,m.id AS message_id,m.source_id,m.posted_at,
                           src.updated_at AS shadow_started_at,s.symbol,s.side,s.order_type,
                           s.entry_low,s.entry_high,s.stop_loss,s.take_profits,s.has_open_runner,
                           s.original_text,
                           COALESCE(pr.style,'unknown') AS provider_style,
                           COALESCE(pr.interpretation_readiness,0) AS interpretation_readiness
                    FROM signals s
                    JOIN messages m ON m.id=s.source_message_id
                    JOIN sources src ON src.id=m.source_id
                    LEFT JOIN provider_research_profiles pr ON pr.source_id=m.source_id
                    WHERE s.id=:signal_id AND s.parser_status='accepted'
                      AND src.status='shadow' AND m.deleted_at IS NULL
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if row is None or row["posted_at"] < row["shadow_started_at"]:
                return False

            stop = _decimal(row["stop_loss"])
            targets = tuple(
                value for value in (_decimal(item) for item in (row["take_profits"] or []))
                if value is not None
            )
            runner = bool(row["has_open_runner"])
            entries = self._research_entries(row)
            if stop is None or not entries or (not targets and not runner) or row["side"] not in {"BUY", "SELL"}:
                return False

            posted_at = row["posted_at"]
            if not isinstance(posted_at, datetime):
                return False
            if posted_at.tzinfo is None:
                posted_at = posted_at.replace(tzinfo=UTC)
            else:
                posted_at = posted_at.astimezone(UTC)

            created_any = False
            for entry in entries:
                shadow_id = session.execute(
                    text(
                        """
                        INSERT INTO shadow_trades(
                            source_id,signal_id,message_id,symbol,side,order_type,entry_low,
                            entry_high,initial_stop,current_stop,take_profits,status,
                            benchmark_model,benchmark_start_balance_usd,benchmark_risk_per_leg_usd,
                            entry_index,entry_order_type,provider_style,interpretation_readiness_at_entry,
                            signal_posted_at,session_bucket,weekday_iso,target_count,score_eligible,
                            score_exclusion_reason,quote_mode
                        ) VALUES (
                            :source_id,:signal_id,:message_id,:symbol,:side,:broad_order_type,:entry_low,
                            :entry_high,:stop,:stop,CAST(:take_profits AS jsonb),'pending',
                            :benchmark_model,:benchmark_balance,:benchmark_risk,:entry_index,:entry_order_type,
                            :provider_style,:readiness,:posted_at,:session_bucket,:weekday_iso,:target_count,
                            false,'market_data_not_observed','unobserved'
                        )
                        ON CONFLICT (signal_id,entry_index) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "source_id": row["source_id"],
                        "signal_id": signal_id,
                        "message_id": row["message_id"],
                        "symbol": str(row["symbol"] or "XAUUSD").upper(),
                        "side": row["side"],
                        "broad_order_type": _broad_order_type(entry.order_type),
                        "entry_low": entry.low,
                        "entry_high": entry.high,
                        "stop": stop,
                        "take_profits": json.dumps([str(value) for value in targets]),
                        "benchmark_model": BENCHMARK_MODEL,
                        "benchmark_balance": BENCHMARK_START_BALANCE_USD,
                        "benchmark_risk": BENCHMARK_RISK_PER_LEG_USD,
                        "entry_index": entry.entry_index,
                        "entry_order_type": entry.order_type,
                        "provider_style": str(row["provider_style"] or "unknown"),
                        "readiness": row["interpretation_readiness"],
                        "posted_at": posted_at,
                        "session_bucket": session_bucket(posted_at),
                        "weekday_iso": posted_at.isoweekday(),
                        "target_count": len(targets) + int(runner),
                    },
                ).scalar_one_or_none()
                if shadow_id is None:
                    continue
                created_any = True
                for index, target in enumerate(targets, start=1):
                    session.execute(
                        text(
                            """
                            INSERT INTO shadow_trade_legs(
                                shadow_trade_id,source_id,signal_id,tp_index,target_price,is_runner
                            ) VALUES (:shadow_trade_id,:source_id,:signal_id,:tp_index,:target,false)
                            ON CONFLICT (shadow_trade_id,tp_index) DO NOTHING
                            """
                        ),
                        {
                            "shadow_trade_id": shadow_id,
                            "source_id": row["source_id"],
                            "signal_id": signal_id,
                            "tp_index": index,
                            "target": target,
                        },
                    )
                if runner:
                    runner_index = len(targets) + 1
                    session.execute(
                        text(
                            """
                            INSERT INTO shadow_trade_legs(
                                shadow_trade_id,source_id,signal_id,tp_index,target_price,is_runner
                            ) VALUES (:shadow_trade_id,:source_id,:signal_id,:tp_index,NULL,true)
                            ON CONFLICT (shadow_trade_id,tp_index) DO NOTHING
                            """
                        ),
                        {
                            "shadow_trade_id": shadow_id,
                            "source_id": row["source_id"],
                            "signal_id": signal_id,
                            "tp_index": runner_index,
                        },
                    )
            session.commit()
            return created_any

    def record_management(self, lifecycle_event_id: UUID) -> bool:
        with self._session_factory() as session:
            event = session.execute(
                text(
                    """
                    SELECT signal_id,aggregate_result
                    FROM signal_lifecycle_events WHERE id=:event_id LIMIT 1
                    """
                ),
                {"event_id": lifecycle_event_id},
            ).mappings().first()
            if event is None:
                return False
            trades = list(
                session.execute(
                    text(
                        """
                        SELECT * FROM shadow_trades
                        WHERE signal_id=:signal_id AND status IN ('pending','open')
                        ORDER BY entry_index
                        """
                    ),
                    {"signal_id": event["signal_id"]},
                ).mappings()
            )
            if not trades:
                return False

            payload = dict(event["aggregate_result"] or {})
            revised = dict(payload.get("revised_instruction") or {})
            actions = list(revised.get("management_actions") or [])
            if not actions and revised.get("update_type"):
                actions = [
                    {
                        "type": revised.get("update_type"),
                        "target": revised.get("update_target") or "all",
                        "value": revised.get("update_value"),
                    }
                ]

            changed = False
            for action in actions:
                kind = str(action.get("type") or "").lower()
                target = str(action.get("target") or "all").lower()
                value = _decimal(action.get("value"))
                selected = self._select_trades(trades, target)
                if not selected:
                    continue

                if kind in {"move_to_break_even", "breakeven"}:
                    for trade in selected:
                        if trade["status"] == "open" and trade["entry_price"] is not None:
                            session.execute(
                                text("UPDATE shadow_trades SET current_stop=entry_price,updated_at=now() WHERE id=:id"),
                                {"id": trade["id"]},
                            )
                            changed = True
                elif kind in {"edit_stop_loss", "move_stop_loss"} and value is not None:
                    for trade in selected:
                        session.execute(
                            text("UPDATE shadow_trades SET current_stop=:value,updated_at=now() WHERE id=:id"),
                            {"id": trade["id"], "value": value},
                        )
                        changed = True
                elif kind in {"edit_take_profit", "move_take_profit"} and value is not None:
                    tp_index = self._target_tp_index(target)
                    if tp_index is not None:
                        for trade in selected:
                            result = session.execute(
                                text(
                                    """
                                    UPDATE shadow_trade_legs
                                    SET target_price=:value,is_runner=false,updated_at=now()
                                    WHERE shadow_trade_id=:trade_id AND tp_index=:tp_index
                                      AND status IN ('pending','open')
                                    """
                                ),
                                {"trade_id": trade["id"], "tp_index": tp_index, "value": value},
                            )
                            changed = bool(result.rowcount) or changed
                elif kind in {"cancel", "cancel_pending"}:
                    for trade in selected:
                        if trade["status"] != "pending":
                            continue
                        session.execute(
                            text(
                                """
                                UPDATE shadow_trades SET status='cancelled',close_reason='provider_cancelled',
                                    closed_at=now(),updated_at=now() WHERE id=:id
                                """
                            ),
                            {"id": trade["id"]},
                        )
                        session.execute(
                            text(
                                """
                                UPDATE shadow_trade_legs SET status='cancelled',remaining_fraction=0,
                                    exit_reason='provider_cancelled',closed_at=now(),updated_at=now()
                                WHERE shadow_trade_id=:id AND status='pending'
                                """
                            ),
                            {"id": trade["id"]},
                        )
                        changed = True
                elif kind in {"close", "close_trade", "close_half"}:
                    partial = kind == "close_half" or "partial" in target
                    fraction = Decimal("0.5") if partial else Decimal("1")
                    changed = self._apply_provider_close(
                        session,
                        selected,
                        target=target,
                        fraction=fraction,
                    ) or changed

            if changed:
                session.commit()
            return changed

    @staticmethod
    def _trade_reference_price(trade: Any) -> Decimal:
        for key in ("entry_price", "entry_low", "entry_high"):
            value = _decimal(trade[key])
            if value is not None:
                return value
        return Decimal("0")

    def _select_trades(self, trades: list[Any], target: str) -> list[Any]:
        active = [trade for trade in trades if not _is_terminal(str(trade["status"]))]
        if not active:
            return []
        normalized = target.lower().strip()
        if normalized in {"all", "remaining", "pending_layers", "partial_tp1", "tp1", "runner"}:
            if normalized == "pending_layers":
                return [trade for trade in active if trade["status"] == "pending"]
            return active

        match = _ENTRY_INDEX.search(normalized)
        if match is not None:
            index = int(match.group(1))
            return [trade for trade in active if int(trade["entry_index"]) == index]

        price_match = _ENTRY_PRICE.search(normalized)
        if price_match is not None:
            target_price = Decimal(price_match.group(1))
            return [trade for trade in active if self._trade_reference_price(trade) == target_price]

        side = str(active[0]["side"])
        ordered = sorted(active, key=self._trade_reference_price, reverse=(side == "BUY"))
        first_match = _FIRST_N.fullmatch(normalized)
        if first_match is not None:
            count = int(first_match.group(1))
            return sorted(active, key=lambda item: int(item["entry_index"]))[:count]
        worst_match = _WORST_N.fullmatch(normalized)
        if worst_match is not None:
            return ordered[: int(worst_match.group(1))]
        if normalized in {"best_entry", "all_but_best"} or normalized.startswith("best_entry_risk_free_"):
            best = ordered[-1]
            if normalized == "all_but_best":
                return [trade for trade in active if trade["id"] != best["id"]]
            return [best]
        return active

    @staticmethod
    def _target_tp_index(target: str) -> int | None:
        match = _TP_INDEX.search(target)
        return int(match.group(1)) if match is not None else None

    def _apply_provider_close(
        self,
        session: Session,
        trades: Iterable[Any],
        *,
        target: str,
        fraction: Decimal,
    ) -> bool:
        changed = False
        tp_index = self._target_tp_index(target)
        runner_only = "runner" in target
        for trade in trades:
            if trade["status"] == "pending":
                if fraction < 1:
                    continue
                session.execute(
                    text(
                        """
                        UPDATE shadow_trades SET status='cancelled',close_reason='provider_closed_before_entry',
                            closed_at=now(),updated_at=now() WHERE id=:id
                        """
                    ),
                    {"id": trade["id"]},
                )
                session.execute(
                    text(
                        """
                        UPDATE shadow_trade_legs SET status='cancelled',remaining_fraction=0,
                            exit_reason='provider_closed_before_entry',closed_at=now(),updated_at=now()
                        WHERE shadow_trade_id=:id AND status='pending'
                        """
                    ),
                    {"id": trade["id"]},
                )
                changed = True
                continue

            entry = _decimal(trade["entry_price"])
            price = _decimal(trade["last_price"])
            initial_stop = _decimal(trade["initial_stop"])
            if entry is None or price is None or initial_stop is None:
                continue
            legs = list(
                session.execute(
                    text(
                        """
                        SELECT * FROM shadow_trade_legs
                        WHERE shadow_trade_id=:trade_id AND status='open'
                        ORDER BY tp_index
                        """
                    ),
                    {"trade_id": trade["id"]},
                ).mappings()
            )
            if tp_index is not None:
                legs = [leg for leg in legs if int(leg["tp_index"]) == tp_index]
            if runner_only:
                legs = [leg for leg in legs if bool(leg["is_runner"])]
            if not legs:
                continue

            # A provider management close on a scalper is scoreable only when the last
            # observed executable price came from the tick stream.
            if str(trade["provider_style"]) == "scalper" and str(trade["quote_mode"]) != "stream_tick":
                session.execute(
                    text(
                        """
                        UPDATE shadow_trades SET score_eligible=false,
                            score_exclusion_reason='scalper_management_exit_requires_tick_resolution'
                        WHERE id=:id
                        """
                    ),
                    {"id": trade["id"]},
                )

            for leg in legs:
                changed = self._realize_leg_fraction(
                    session,
                    leg=leg,
                    entry=entry,
                    initial_stop=initial_stop,
                    side=str(trade["side"]),
                    exit_price=price,
                    fraction=fraction,
                    final_reason="provider_close",
                ) or changed
            self._aggregate_trade(session, trade["id"], preferred_reason="provider_close")
        return changed

    @staticmethod
    def _realize_leg_fraction(
        session: Session,
        *,
        leg: Any,
        entry: Decimal,
        initial_stop: Decimal,
        side: str,
        exit_price: Decimal,
        fraction: Decimal,
        final_reason: str,
    ) -> bool:
        remaining = _decimal(leg["remaining_fraction"]) or Decimal("0")
        if remaining <= 0 or fraction <= 0:
            return False
        close_fraction = remaining if fraction >= 1 else remaining * fraction
        if close_fraction <= 0:
            return False
        prior_r = _decimal(leg["quality_r"]) or Decimal("0")
        realized_r = _leg_r(
            entry=entry,
            exit_price=exit_price,
            initial_stop=initial_stop,
            side=side,
        ) * close_fraction
        new_r = prior_r + realized_r
        new_remaining = remaining - close_fraction
        terminal = new_remaining <= _EPSILON
        if terminal:
            new_remaining = Decimal("0")
        session.execute(
            text(
                """
                UPDATE shadow_trade_legs
                SET remaining_fraction=:remaining,quality_r=:quality_r,
                    benchmark_pnl_usd=:pnl,
                    status=CASE WHEN :terminal THEN 'closed' ELSE status END,
                    exit_reason=CASE WHEN :terminal THEN :reason ELSE exit_reason END,
                    exit_price=CASE WHEN :terminal THEN :exit_price ELSE exit_price END,
                    closed_at=CASE WHEN :terminal THEN now() ELSE closed_at END,
                    updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": leg["id"],
                "remaining": new_remaining,
                "quality_r": new_r,
                "pnl": _benchmark_pnl_usd(new_r),
                "terminal": terminal,
                "reason": final_reason,
                "exit_price": exit_price,
            },
        )
        return True

    @staticmethod
    def _aggregate_trade(session: Session, trade_id: UUID, *, preferred_reason: str | None = None) -> None:
        rows = list(
            session.execute(
                text("SELECT * FROM shadow_trade_legs WHERE shadow_trade_id=:id ORDER BY tp_index"),
                {"id": trade_id},
            ).mappings()
        )
        if not rows:
            return
        quality_r = sum((_decimal(row["quality_r"]) or Decimal("0") for row in rows), Decimal("0"))
        pnl = sum((_decimal(row["benchmark_pnl_usd"]) or Decimal("0") for row in rows), Decimal("0"))
        hit_targets = [
            int(row["tp_index"])
            for row in rows
            if row["status"] == "closed" and row["exit_reason"] == "target" and not bool(row["is_runner"])
        ]
        active = [row for row in rows if row["status"] in {"pending", "open"}]
        all_cancelled = all(row["status"] == "cancelled" for row in rows)
        terminal = not active
        if all_cancelled:
            status = "cancelled"
        elif terminal:
            status = "closed"
        else:
            status = None

        reasons = {str(row["exit_reason"] or "") for row in rows}
        reason = preferred_reason
        if reason is None and terminal:
            if "shadow_stop" in reasons:
                reason = "shadow_stop"
            elif reasons and reasons <= {"target", ""}:
                reason = "all_targets_hit"
            elif "provider_close" in reasons:
                reason = "provider_close"

        session.execute(
            text(
                """
                UPDATE shadow_trades
                SET hit_targets=CAST(:hit AS jsonb),quality_r_multiple=:quality_r,
                    benchmark_pnl_usd=:pnl,realized_percent=:quality_r,
                    pnl_percent=CASE WHEN :terminal AND :status='closed' THEN :quality_r ELSE pnl_percent END,
                    status=COALESCE(:status,status),
                    closed_at=CASE WHEN :terminal THEN COALESCE(closed_at,now()) ELSE closed_at END,
                    close_reason=COALESCE(:reason,close_reason),updated_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": trade_id,
                "hit": json.dumps(sorted(hit_targets)),
                "quality_r": quality_r,
                "pnl": pnl,
                "terminal": terminal,
                "status": status,
                "reason": reason,
            },
        )


class _GoldQuoteListener(SynchronizationListener):
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

    async def on_ticks_updated(self, instance_index: int, ticks: list[Any], **kwargs: Any) -> None:
        del instance_index, kwargs
        for tick in ticks:
            if str(tick.get("symbol") or "").upper() != "XAUUSD":
                continue
            await self._manager._handle_stream_price(
                bid=_decimal(tick.get("bid")),
                ask=_decimal(tick.get("ask")),
                quote_mode="stream_tick",
            )


class ShadowTradeManager:
    """Evaluate all shadow providers from one shared XAUUSD stream plus REST fallback."""

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
        self._stream_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._evaluation_lock = asyncio.Lock()
        self._last_stream_monotonic = 0.0
        self._stream_connection: Any | None = None
        self._stream_api: Any | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="super-signals-shadow-fallback")
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = asyncio.create_task(self._run_stream(), name="super-signals-shadow-stream")

    async def stop(self) -> None:
        self._stopping.set()
        for task in (self._stream_task, self._task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._stream_task, self._task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._stream_task = None
        self._task = None
        await self._close_stream_resources()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Shadow fallback evaluator failed safely")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

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
                listener = _GoldQuoteListener(self)
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
                logger.info("Provider Lab XAUUSD streaming market data connected")
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

    async def _close_stream_resources(self) -> None:
        connection = self._stream_connection
        api = self._stream_api
        self._stream_connection = None
        self._stream_api = None
        if connection is not None:
            try:
                result = connection.close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Provider Lab stream close failed safely", exc_info=True)
        if api is not None:
            close = getattr(api, "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.debug("Provider Lab MetaApi SDK close failed safely", exc_info=True)

    async def _handle_stream_price(
        self,
        *,
        bid: Decimal | None,
        ask: Decimal | None,
        quote_mode: str,
    ) -> None:
        if bid is None or ask is None:
            return
        self._last_stream_monotonic = time.monotonic()
        await self._evaluate_all(bid=bid, ask=ask, quote_mode=quote_mode)

    async def poll_once(self) -> int:
        # Do not spend REST calls while the websocket stream is healthy. The fallback
        # exists only to keep slower providers observable during a stream interruption.
        if time.monotonic() - self._last_stream_monotonic < max(5, self._poll_seconds):
            return 0
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
        return await self._evaluate_all(bid=bid, ask=ask, quote_mode="snapshot_poll")

    async def _evaluate_all(self, *, bid: Decimal, ask: Decimal, quote_mode: str) -> int:
        async with self._evaluation_lock:
            rows = self._active_rows()
            if not rows:
                return 0
            changed = 0
            with self._session_factory() as session:
                for row in rows:
                    changed += int(
                        self._evaluate_row(
                            session,
                            row,
                            bid=bid,
                            ask=ask,
                            quote_mode=quote_mode,
                        )
                    )
                session.commit()
            return changed

    def _active_rows(self) -> list[Any]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        "SELECT * FROM shadow_trades "
                        "WHERE status IN ('pending','open') ORDER BY created_at,entry_index"
                    )
                ).mappings()
            )

    def _broker_account(self) -> tuple[str, bytes] | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT metaapi_account_id,metaapi_token_ciphertext FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND account_environment='demo' AND status='connected'
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"user_id": self._owner_user_id},
            ).mappings().first()
        return None if row is None else (
            str(row["metaapi_account_id"]),
            bytes(row["metaapi_token_ciphertext"]),
        )

    @staticmethod
    def _crossed_zone(previous: Decimal | None, price: Decimal, low: Decimal, high: Decimal) -> bool:
        if low <= price <= high:
            return True
        if previous is None:
            return False
        path_low = min(previous, price)
        path_high = max(previous, price)
        return path_high >= low and path_low <= high

    @staticmethod
    def _entry_fill_price(*, entry_order_type: str, price: Decimal, low: Decimal, high: Decimal) -> Decimal:
        if entry_order_type == "market":
            return price
        # For pending orders/zones, the observed executable price is deliberately used
        # so gaps/slippage are not hidden by an unrealistically perfect provider fill.
        return price if low <= price <= high else (low if price < low else high)

    def _mark_ineligible(self, session: Session, trade_id: UUID, reason: str) -> None:
        session.execute(
            text(
                """
                UPDATE shadow_trades SET score_eligible=false,score_exclusion_reason=:reason,
                    updated_at=now() WHERE id=:id
                """
            ),
            {"id": trade_id, "reason": reason},
        )

    def _evaluate_row(
        self,
        session: Session,
        row: Any,
        *,
        bid: Decimal,
        ask: Decimal,
        quote_mode: str,
    ) -> bool:
        side = str(row["side"])
        price = ask if side == "BUY" else bid
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
            triggered = immediate_market or self._crossed_zone(previous, price, min(low, high), max(low, high))
            passed_stop = (side == "BUY" and price <= stop) or (side == "SELL" and price >= stop)

            # If a coarse snapshot jumped across both entry and stop, sequence is unknown.
            # Preserve the event but never score it as a loss or win.
            if triggered and passed_stop and quote_mode == "snapshot_poll" and not immediate_market:
                session.execute(
                    text(
                        """
                        UPDATE shadow_trades SET status='missed',quote_mode=:mode,score_eligible=false,
                            score_exclusion_reason='entry_stop_sequence_ambiguous_snapshot',
                            close_reason='entry_stop_sequence_ambiguous_snapshot',last_price=:price,
                            closed_at=now(),updated_at=now() WHERE id=:id
                        """
                    ),
                    {"id": row["id"], "mode": quote_mode, "price": price},
                )
                session.execute(
                    text(
                        """
                        UPDATE shadow_trade_legs SET status='cancelled',remaining_fraction=0,
                            exit_reason='entry_sequence_ambiguous',closed_at=now(),updated_at=now()
                        WHERE shadow_trade_id=:id AND status='pending'
                        """
                    ),
                    {"id": row["id"]},
                )
                return True

            if passed_stop and not triggered:
                session.execute(
                    text(
                        """
                        UPDATE shadow_trades SET status='missed',quote_mode=:mode,score_eligible=false,
                            score_exclusion_reason='price_passed_stop_before_entry',
                            close_reason='price_passed_stop_before_entry',last_price=:price,
                            closed_at=now(),updated_at=now() WHERE id=:id
                        """
                    ),
                    {"id": row["id"], "mode": quote_mode, "price": price},
                )
                session.execute(
                    text(
                        """
                        UPDATE shadow_trade_legs SET status='cancelled',remaining_fraction=0,
                            exit_reason='never_entered',closed_at=now(),updated_at=now()
                        WHERE shadow_trade_id=:id AND status='pending'
                        """
                    ),
                    {"id": row["id"]},
                )
                return True

            if not triggered:
                session.execute(
                    text(
                        "UPDATE shadow_trades SET last_price=:price,quote_mode=:mode,updated_at=now() WHERE id=:id"
                    ),
                    {"id": row["id"], "price": price, "mode": quote_mode},
                )
                return True

            entry = self._entry_fill_price(
                entry_order_type=entry_order_type,
                price=price,
                low=min(low, high),
                high=max(low, high),
            )
            eligible, reason = score_eligibility(
                style=str(row["provider_style"]),
                quote_mode=quote_mode,
            )
            posted_at = row["signal_posted_at"]
            delay_ms: int | None = None
            if isinstance(posted_at, datetime):
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=UTC)
                delay_ms = max(0, int((datetime.now(UTC) - posted_at.astimezone(UTC)).total_seconds() * 1000))
            session.execute(
                text(
                    """
                    UPDATE shadow_trades SET status='open',entry_price=:entry,last_price=:price,
                        opened_at=now(),max_price=:price,min_price=:price,quote_mode=:mode,
                        score_eligible=:eligible,score_exclusion_reason=:reason,
                        entry_delay_ms=:delay_ms,entry_spread=:spread,updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": row["id"],
                    "entry": entry,
                    "price": price,
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
            {"id": row["id"], "price": price, "mode": quote_mode},
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
        target_hit = False
        for leg in legs:
            if bool(leg["is_runner"]):
                continue
            target = _decimal(leg["target_price"])
            if target is None:
                continue
            reached = price >= target if side == "BUY" else price <= target
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
            target_hit = True

        stopped = price <= stop if side == "BUY" else price >= stop
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

        if changed or target_hit or stopped:
            ShadowTradeService._aggregate_trade(
                session,
                row["id"],
                preferred_reason=("shadow_stop" if stopped else None),
            )
        return True


__all__ = [
    "ShadowTradeManager",
    "ShadowTradeService",
    "_benchmark_pnl_usd",
    "_decimal",
    "_leg_r",
]
