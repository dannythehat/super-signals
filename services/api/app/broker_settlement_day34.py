"""Day 34 broker-led settlement watcher.

Providers are not required to announce TP/SL/final results. This read-only manager polls
the existing Day 33 broker ledger for the configured reference account, reconciles
terminal broker outcomes into local position state, and creates exactly-once canonical
broker lifecycle/result events for Day-34-forward settlements. It never places, closes
or modifies a broker trade.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.models import AuditEvent
from app.performance_ledger_day33 import Day33LedgerError
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Day34SettlementPollResult:
    synced: bool
    positions_reconciled: int
    position_events_created: int
    signal_results_created: int
    reason: str
    broker_trade_action_created: bool = False


class Day34BrokerSettlementManager:
    """Continuously turn broker truth into canonical lifecycle completion evidence."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        performance_service: Day33PerformanceLedgerServiceV2,
        reference_user_id: UUID,
        poll_seconds: int = 15,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("day34_settlement_poll_seconds_invalid")
        self._session_factory = session_factory
        self._performance = performance_service
        self._reference_user_id = reference_user_id
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="day34-broker-settlement-watch")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    def _poll_once_isolated(self) -> Day34SettlementPollResult:
        """Run one broker-settlement pass on its own worker event loop.

        The settlement service mixes synchronous SQLAlchemy reconciliation with async
        MetaAPI reads. Keeping the whole pass off Uvicorn's event loop prevents a slow
        broker/database read from starving /health, Telegram intake, or trade dispatch.
        """
        return asyncio.run(self.poll_once())

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.to_thread(self._poll_once_isolated)
            except Exception:
                # Read-side settlement monitoring must never take down Telegram,
                # execution, the application, or broker-held SL/TP protection.
                logger.exception("Day 34 broker settlement watch failed safely")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

    async def poll_once(self) -> Day34SettlementPollResult:
        if not self._has_unsettled_mapped_positions():
            return Day34SettlementPollResult(
                synced=False,
                positions_reconciled=0,
                position_events_created=0,
                signal_results_created=0,
                reason="no_unsettled_mapped_positions",
            )

        sync_ok = True
        sync_reason = "broker_settlement_sync_complete"
        try:
            # A single slow MetaAPI history request must never stall a nominal
            # 15-second settlement watcher for minutes. Current/open positions are
            # prioritized by the Day 33 ledger; cap the whole broker read pass and
            # still rebuild from any broker deals that were committed before timeout.
            await asyncio.wait_for(
                self._performance.sync_user(self._reference_user_id),
                timeout=12.0,
            )
        except TimeoutError:
            sync_ok = False
            sync_reason = "broker_settlement_sync_timeout"
            self._audit_poll_failure(sync_reason, retryable=True)
            self._performance.rebuild_outcomes(self._reference_user_id)
        except Day33LedgerError as exc:
            sync_ok = False
            sync_reason = exc.code
            self._audit_poll_failure(exc.code, retryable=exc.retryable)
            # Preserve any immutable broker evidence already stored before a later
            # request failed, then continue local settlement reconciliation.
            self._performance.rebuild_outcomes(self._reference_user_id)

        # Historical reconciliation is intentionally allowed so stale local rows can be
        # corrected from broker truth. Member-facing lifecycle/result events below are
        # cut over separately so deployment cannot replay old Day 26-33 outcomes.
        reconciled = self._reconcile_terminal_positions()
        position_events = self._create_position_settlement_events()
        signal_results = self._create_signal_result_events()
        self._audit_poll_success(
            positions_reconciled=reconciled,
            position_events_created=position_events,
            signal_results_created=signal_results,
        )
        return Day34SettlementPollResult(
            synced=sync_ok,
            positions_reconciled=reconciled,
            position_events_created=position_events,
            signal_results_created=signal_results,
            reason=sync_reason,
        )

    def _has_unsettled_mapped_positions(self) -> bool:
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text(
                        """
                        SELECT 1
                        FROM positions AS p
                        WHERE p.user_id = :user_id
                          AND p.broker_position_id IS NOT NULL
                          AND (
                              p.status = 'open'
                              OR NOT EXISTS (
                                  SELECT 1
                                  FROM performance_trade_outcomes AS o
                                  WHERE o.position_id = p.id
                                    AND o.status IN ('won','lost','breakeven','closed_unknown')
                              )
                          )
                        LIMIT 1
                        """
                    ),
                    {"user_id": self._reference_user_id},
                ).scalar_one_or_none()
            )

    def _reconcile_terminal_positions(self) -> int:
        """Close local rows only after the broker-derived Day 33 outcome is terminal."""
        now = datetime.now(UTC)
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    UPDATE positions AS p
                    SET status = 'closed',
                        closed_at = COALESCE(p.closed_at, o.closed_at, :now),
                        exit_price = COALESCE(p.exit_price, o.exit_price),
                        pnl_amount = COALESCE(p.pnl_amount, o.cash_pnl),
                        close_reason = COALESCE(p.close_reason, 'broker_settled'),
                        updated_at = :now
                    FROM performance_trade_outcomes AS o
                    WHERE o.position_id = p.id
                      AND p.user_id = :user_id
                      AND p.status = 'open'
                      AND o.status IN ('won','lost','breakeven','closed_unknown')
                    RETURNING p.id, p.signal_id, p.tp_index, o.status,
                              o.closed_at, o.net_pips, o.model_500_pnl
                    """
                ),
                {"user_id": self._reference_user_id, "now": now},
            ).mappings().all()
            for row in rows:
                session.add(
                    AuditEvent(
                        actor_user_id=self._reference_user_id,
                        event_type="mt5.day34_broker_settlement_reconciled",
                        entity_type="position",
                        entity_id=row["id"],
                        payload={
                            "signal_id": str(row["signal_id"]),
                            "tp_index": int(row["tp_index"]),
                            "broker_outcome": str(row["status"]),
                            "broker_closed_at": (
                                row["closed_at"].isoformat()
                                if isinstance(row["closed_at"], datetime)
                                else str(row["closed_at"] or "")
                            ),
                            "net_pips": self._plain(row["net_pips"]),
                            "model_500_pnl": self._plain(row["model_500_pnl"]),
                            "provider_message_required": False,
                            "trade_action_created": False,
                        },
                    )
                )
            session.commit()
            return len(rows)

    def _publish_after(self, session: Session) -> datetime:
        value = session.execute(
            text("SELECT publish_after FROM day34_summary_state WHERE id = 1")
        ).scalar_one()
        if not isinstance(value, datetime):
            raise RuntimeError("day34_notification_cutover_missing")
        return value

    def _create_position_settlement_events(self) -> int:
        """Create one broker lifecycle event for each Day-34-forward terminal TP leg."""
        with self._session_factory() as session:
            publish_after = self._publish_after(session)
            rows = session.execute(
                text(
                    """
                    SELECT
                        o.position_id,
                        o.signal_id,
                        o.status,
                        o.net_pips,
                        o.model_500_pnl,
                        o.closed_at,
                        p.tp_index,
                        s.symbol
                    FROM performance_trade_outcomes AS o
                    JOIN positions AS p ON p.id = o.position_id
                    JOIN signals AS s ON s.id = o.signal_id
                    WHERE o.user_id = :user_id
                      AND o.status IN ('won','lost','breakeven','closed_unknown')
                      AND o.closed_at IS NOT NULL
                      AND o.closed_at > :publish_after
                      AND NOT EXISTS (
                          SELECT 1
                          FROM signal_lifecycle_events AS ev
                          WHERE ev.event_key = 'broker-position-settled:' || o.position_id::text
                      )
                    ORDER BY o.closed_at, p.tp_index
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "publish_after": publish_after,
                },
            ).mappings().all()

            created = 0
            for row in rows:
                event_key = f"broker-position-settled:{row['position_id']}"
                status = str(row["status"])
                pips = row["net_pips"]
                label = f"TP{int(row['tp_index'])} position closed"
                if status == "won":
                    prefix = "✅"
                elif status == "lost":
                    prefix = "❌"
                else:
                    prefix = "➖"
                pips_text = self._signed(pips, suffix=" pips") if pips is not None else None
                rendered = f"{prefix} {str(row['symbol']).upper()} · {label}"
                if pips_text:
                    rendered += f"\n{pips_text}"

                inserted = session.execute(
                    text(
                        """
                        INSERT INTO signal_lifecycle_events (
                            signal_id, source_message_id, source_revision_index,
                            event_type, event_key, origin, rendered_text, pips,
                            aggregate_result, occurred_at
                        ) VALUES (
                            :signal_id, NULL, NULL,
                            'broker_position_settled', :event_key, 'broker',
                            :rendered_text, :pips, CAST(:aggregate_result AS jsonb),
                            :occurred_at
                        )
                        ON CONFLICT (event_key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "signal_id": row["signal_id"],
                        "event_key": event_key,
                        "rendered_text": rendered,
                        "pips": pips,
                        "aggregate_result": json.dumps(
                            {
                                "day34_broker_settlement": True,
                                "position_id": str(row["position_id"]),
                                "tp_index": int(row["tp_index"]),
                                "outcome": status,
                                "net_pips": self._plain(pips),
                                "model_500_pnl": self._plain(row["model_500_pnl"]),
                                "provider_message_required": False,
                                "real_user_balance_exposed": False,
                            }
                        ),
                        "occurred_at": row["closed_at"],
                    },
                ).scalar_one_or_none()
                if inserted is not None:
                    created += 1
            session.commit()
            return created

    def _create_signal_result_events(self) -> int:
        """Create one final plain-English result when a Day-34-forward trade settles."""
        with self._session_factory() as session:
            publish_after = self._publish_after(session)
            rows = session.execute(
                text(
                    """
                    SELECT
                        o.signal_id,
                        MAX(s.symbol) AS symbol,
                        MAX(s.side) AS side,
                        MAX(o.closed_at) AS closed_at,
                        SUM(o.model_500_pnl) FILTER (WHERE o.model_500_pnl IS NOT NULL) AS model_500_pnl,
                        CASE
                            WHEN COUNT(*) FILTER (WHERE o.net_pips IS NULL) = 0
                            THEN SUM(o.net_pips)
                            ELSE NULL
                        END AS net_pips,
                        COUNT(*)::int AS position_count,
                        COUNT(*) FILTER (
                            WHERE o.status IN ('won','lost','breakeven','closed_unknown')
                        )::int AS terminal_count,
                        COUNT(*) FILTER (WHERE o.status IN ('open','pending'))::int AS active_count,
                        BOOL_OR(o.status = 'closed_unknown') AS has_unknown
                    FROM performance_trade_outcomes AS o
                    JOIN signals AS s ON s.id = o.signal_id
                    WHERE o.user_id = :user_id
                    GROUP BY o.signal_id
                    HAVING COUNT(*) > 0
                       AND COUNT(*) FILTER (WHERE o.status IN ('open','pending')) = 0
                       AND COUNT(*) FILTER (
                           WHERE o.status IN ('won','lost','breakeven','closed_unknown')
                       ) = COUNT(*)
                       AND COUNT(*) FILTER (WHERE o.closed_at IS NULL) = 0
                       AND MAX(o.closed_at) > :publish_after
                    ORDER BY MAX(o.closed_at)
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "publish_after": publish_after,
                },
            ).mappings().all()

            created = 0
            for row in rows:
                event_key = f"broker-signal-settled:{row['signal_id']}"
                if session.execute(
                    text("SELECT 1 FROM signal_lifecycle_events WHERE event_key=:event_key"),
                    {"event_key": event_key},
                ).scalar_one_or_none():
                    continue

                model_pnl = row["model_500_pnl"]
                if model_pnl is None or bool(row["has_unknown"]):
                    outcome = "closed"
                    event_type = "broker_result_closed"
                    icon = "✅"
                elif Decimal(str(model_pnl)) > 0:
                    outcome = "win"
                    event_type = "broker_result_win"
                    icon = "🎆"
                elif Decimal(str(model_pnl)) < 0:
                    outcome = "loss"
                    event_type = "broker_result_loss"
                    icon = "❌"
                else:
                    outcome = "breakeven"
                    event_type = "broker_result_breakeven"
                    icon = "➖"

                symbol = str(row["symbol"] or "").upper()
                side = str(row["side"] or "").upper()
                heading = (
                    "🎉🎉 TRADE CLOSED — WIN 🎉🎉"
                    if outcome == "win"
                    else "❌ TRADE CLOSED — LOSS"
                    if outcome == "loss"
                    else "➖ TRADE CLOSED — BREAK EVEN"
                    if outcome == "breakeven"
                    else "✅ TRADE CLOSED"
                )
                lines = [heading, f"{symbol} {side}".strip()]
                if row["net_pips"] is not None:
                    result_label = (
                        "PROFIT" if outcome == "win"
                        else "LOSS" if outcome == "loss"
                        else "RESULT"
                    )
                    lines.append(
                        f"{result_label}: {self._signed(row['net_pips'], suffix=' pips')}"
                    )
                if model_pnl is not None:
                    example_label = (
                        "EXAMPLE PROFIT" if outcome == "win"
                        else "EXAMPLE LOSS" if outcome == "loss"
                        else "$500 EXAMPLE"
                    )
                    lines.append(
                        f"{example_label} at Recommended 1%: "
                        + self._signed(model_pnl, prefix="$", money=True)
                    )
                rendered = "\n".join(lines)

                inserted = session.execute(
                    text(
                        """
                        INSERT INTO signal_lifecycle_events (
                            signal_id, source_message_id, source_revision_index,
                            event_type, event_key, origin, rendered_text, pips,
                            aggregate_result, occurred_at
                        ) VALUES (
                            :signal_id, NULL, NULL,
                            :event_type, :event_key, 'broker', :rendered_text, :pips,
                            CAST(:aggregate_result AS jsonb), :occurred_at
                        )
                        ON CONFLICT (event_key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "signal_id": row["signal_id"],
                        "event_type": event_type,
                        "event_key": event_key,
                        "rendered_text": rendered,
                        "pips": row["net_pips"],
                        "aggregate_result": json.dumps(
                            {
                                "day34_final_result": True,
                                "broker_confirmed": True,
                                "provider_message_required": False,
                                "outcome": outcome,
                                "position_count": int(row["position_count"]),
                                "net_pips": self._plain(row["net_pips"]),
                                "model_balance": "500",
                                "recommended_risk_percent": "1",
                                "model_500_pnl": self._plain(model_pnl),
                                "real_user_balance_exposed": False,
                                "real_user_pnl_exposed": False,
                            }
                        ),
                        "occurred_at": row["closed_at"],
                    },
                ).scalar_one_or_none()
                if inserted is not None:
                    created += 1
            session.commit()
            return created

    def _audit_poll_success(
        self,
        *,
        positions_reconciled: int,
        position_events_created: int,
        signal_results_created: int,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=self._reference_user_id,
                    event_type="mt5.day34_settlement_poll_complete",
                    entity_type="mt5_account",
                    entity_id=None,
                    payload={
                        "positions_reconciled": positions_reconciled,
                        "position_events_created": position_events_created,
                        "signal_results_created": signal_results_created,
                        "broker_truth_authoritative": True,
                        "provider_message_required": False,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    def _audit_poll_failure(self, code: str, *, retryable: bool) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=self._reference_user_id,
                    event_type="mt5.day34_settlement_poll_failed",
                    entity_type="mt5_account",
                    entity_id=None,
                    payload={
                        "error_code": code,
                        "retryable": retryable,
                        "trading_unchanged": True,
                        "trade_action_created": False,
                    },
                )
            )
            session.commit()

    @staticmethod
    def _plain(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        return str(value)

    @staticmethod
    def _signed(
        value: Any,
        *,
        prefix: str = "",
        suffix: str = "",
        money: bool = False,
    ) -> str:
        amount = Decimal(str(value))
        if money:
            body = f"{abs(amount):.2f}"
        else:
            body = format(abs(amount).normalize(), "f")
        sign = "+" if amount > 0 else "-" if amount < 0 else ""
        return f"{sign}{prefix}{body}{suffix}"


__all__ = ["Day34BrokerSettlementManager", "Day34SettlementPollResult"]
