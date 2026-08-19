"""Production performance view over the canonical broker-account ledger.

Accounting contract:
* one provider Signal is one trade, regardless of TP tranche count;
* only fully broker-decided Signals are Won/Lost/BE headline trades;
* any ``closed_unknown`` leg is settlement diagnostics, never a completed trade;
* cash/P&L and pips come only from broker-decided legs;
* open/pending state is reported separately;
* Today is the configured local calendar day (Europe/Sofia by default), never UTC
  midnight unless explicitly configured that way.

The underlying account/deal synchronisation remains CanonicalPerformanceLedgerService.
This subclass changes only the derived performance view and summary aggregation.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from app.dashboard_today_summary import local_day_bounds
from app.performance_ledger_canonical import CanonicalPerformanceLedgerService
from app.performance_ledger_day33 import (
    MODEL_BALANCE,
    Day33PerformanceWindow,
    _d,
    _money,
    _pct,
)

_DEFAULT_TIMEZONE = "Europe/Sofia"
_DECIDED = {"won", "lost", "breakeven"}
_UNRESOLVED = {"open", "pending", "closed_unknown"}


class CanonicalPerformanceRuntimeService(CanonicalPerformanceLedgerService):
    """Single production dashboard/summary performance contract."""

    @staticmethod
    def _timezone_name() -> str:
        return (
            os.getenv("SUPER_SIGNALS_DASHBOARD_TIMEZONE", _DEFAULT_TIMEZONE).strip()
            or _DEFAULT_TIMEZONE
        )

    def read_windows(
        self,
        user_id: UUID,
        *,
        now: datetime | None = None,
    ) -> tuple[Day33PerformanceWindow, ...]:
        point = now or datetime.now(UTC)
        if point.tzinfo is None:
            point = point.replace(tzinfo=UTC)
        point = point.astimezone(UTC)

        resolved_timezone, start_today, _ = local_day_bounds(
            self._timezone_name(),
            now_utc=point,
        )
        try:
            zone = ZoneInfo(resolved_timezone)
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("UTC")
        local_now = point.astimezone(zone)
        local_month = local_now.replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        start_month = local_month.astimezone(UTC)

        windows = (
            ("today", "Today", start_today),
            ("7d", "7 days", point - timedelta(days=7)),
            ("30d", "30 days", point - timedelta(days=30)),
            ("month", "Month", start_month),
            ("all", "All time", None),
        )
        return tuple(
            self._signal_window(user_id, key, label, since, point)
            for key, label, since in windows
        )

    def _signal_window(
        self,
        user_id: UUID,
        key: str,
        label: str,
        since: datetime | None,
        now: datetime,
    ) -> Day33PerformanceWindow:
        clauses = ["o.user_id=:user_id"]
        params: dict[str, Any] = {"user_id": user_id, "window_end": now}
        if since is not None:
            clauses.append(
                "(o.closed_at>=:since OR "
                "(o.status IN ('open','pending','closed_unknown') AND "
                "COALESCE(o.opened_at,o.closed_at,o.derived_at)>=:since))"
            )
            params["since"] = since
        clauses.append("COALESCE(o.closed_at,o.opened_at,o.derived_at)<:window_end")

        with self._session_factory() as session:
            rows = session.execute(
                text(
                    f"""
                    SELECT
                        o.signal_id,
                        MAX(o.symbol) AS symbol,
                        COUNT(*)::int AS leg_count,
                        COUNT(*) FILTER (WHERE o.status IN ('won','lost','breakeven'))::int
                            AS decided_legs,
                        COUNT(*) FILTER (WHERE o.status='won')::int AS won_legs,
                        COUNT(*) FILTER (WHERE o.status='lost')::int AS lost_legs,
                        COUNT(*) FILTER (WHERE o.status='breakeven')::int AS be_legs,
                        COUNT(*) FILTER (WHERE o.status='open')::int AS open_legs,
                        COUNT(*) FILTER (WHERE o.status='pending')::int AS pending_legs,
                        COUNT(*) FILTER (WHERE o.status='closed_unknown')::int AS unknown_legs,
                        SUM(o.cash_pnl) FILTER (
                            WHERE o.status IN ('won','lost','breakeven')
                              AND o.cash_pnl IS NOT NULL
                        ) AS cash_pnl,
                        SUM(o.model_500_pnl) FILTER (
                            WHERE o.status IN ('won','lost','breakeven')
                              AND o.model_500_pnl IS NOT NULL
                        ) AS model_pnl,
                        SUM(o.net_pips) FILTER (
                            WHERE o.status IN ('won','lost','breakeven')
                              AND o.net_pips IS NOT NULL
                        ) AS net_pips,
                        COUNT(*) FILTER (
                            WHERE o.status IN ('won','lost','breakeven')
                              AND o.net_pips IS NULL
                        )::int AS missing_pip_legs
                    FROM performance_trade_outcomes AS o
                    WHERE {' AND '.join(clauses)}
                    GROUP BY o.signal_id
                    """
                ),
                params,
            ).mappings().all()

        wins = losses = breakeven = 0
        open_trades = 0
        realised_cash = Decimal("0")
        model_cash = Decimal("0")
        realised_pips = Decimal("0")
        pips_available = False
        pips_complete = True
        symbols: set[str] = set()

        for row in rows:
            leg_count = int(row["leg_count"] or 0)
            decided = int(row["decided_legs"] or 0)
            open_legs = int(row["open_legs"] or 0)
            pending_legs = int(row["pending_legs"] or 0)
            unknown_legs = int(row["unknown_legs"] or 0)

            if row["cash_pnl"] is not None:
                realised_cash += _d(row["cash_pnl"])
            if row["model_pnl"] is not None:
                model_cash += _d(row["model_pnl"])
            if decided:
                symbols.add(str(row["symbol"] or ""))
                if int(row["missing_pip_legs"] or 0):
                    pips_complete = False
                elif row["net_pips"] is not None:
                    realised_pips += _d(row["net_pips"])
                    pips_available = True

            # A Signal is closed for headline purposes only when every represented leg
            # has broker truth. ``closed_unknown`` is settlement debt, not a trade.
            fully_decided = leg_count > 0 and decided == leg_count and not (
                open_legs or pending_legs or unknown_legs
            )
            if fully_decided:
                if int(row["lost_legs"] or 0) > 0:
                    losses += 1
                elif int(row["won_legs"] or 0) > 0:
                    wins += 1
                else:
                    breakeven += 1
            elif open_legs or pending_legs:
                open_trades += 1

        cash = _money(realised_cash)
        model = _money(model_cash)
        decided_trades = wins + losses
        win_rate = (
            _pct(Decimal(wins) / Decimal(decided_trades) * Decimal("100"))
            if decided_trades
            else None
        )
        mixed = len({symbol for symbol in symbols if symbol}) > 1
        net_pips = (
            _money(realised_pips)
            if pips_available and pips_complete and not mixed
            else None
        )
        period_start = since or datetime(1970, 1, 1, tzinfo=UTC)
        return_percent = self._period_return_percent(user_id, period_start, cash)

        return Day33PerformanceWindow(
            key=key,
            label=label,
            cash_pnl=cash,
            return_percent=return_percent,
            model_500_pnl=model,
            model_500_return_percent=_pct(model / MODEL_BALANCE * Decimal("100")),
            closed_trades=wins + losses + breakeven,
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            open_trades=open_trades,
            win_rate_percent=win_rate,
            net_pips=net_pips,
            mixed_instrument_pips=mixed,
        )

    def _summary_metrics(
        self,
        user_id: UUID,
        rows: list[Any],
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Build persisted summaries as one Signal = one trade."""
        by_signal: dict[UUID, list[Any]] = {}
        for row in rows:
            by_signal.setdefault(row["signal_id"], []).append(row)

        wins = losses = breakeven = open_trades = 0
        cash = Decimal("0")
        model = Decimal("0")
        pip_values: list[Decimal] = []
        pips_complete = True
        symbols: set[str] = set()
        digests: list[str] = []

        for signal_rows in by_signal.values():
            decided = [row for row in signal_rows if str(row["status"]) in _DECIDED]
            unresolved = [row for row in signal_rows if str(row["status"]) in _UNRESOLVED]
            for row in decided:
                if row["cash_pnl"] is not None:
                    cash += _d(row["cash_pnl"])
                if row["model_500_pnl"] is not None:
                    model += _d(row["model_500_pnl"])
                if row["net_pips"] is None:
                    pips_complete = False
                else:
                    pip_values.append(_d(row["net_pips"]))
                    symbols.add(str(row["symbol"] or ""))
            digests.extend(str(row["source_digest"]) for row in signal_rows)

            fully_decided = len(decided) == len(signal_rows) and not unresolved
            if fully_decided and decided:
                statuses = {str(row["status"]) for row in decided}
                if "lost" in statuses:
                    losses += 1
                elif "won" in statuses:
                    wins += 1
                else:
                    breakeven += 1
            elif any(str(row["status"]) in {"open", "pending"} for row in signal_rows):
                open_trades += 1

        cash = _money(cash)
        model = _money(model)
        net_pips = gross_profit_pips = gross_loss_pips = None
        mixed = len({symbol for symbol in symbols if symbol}) > 1
        if pip_values and pips_complete and not mixed:
            net_pips = _money(sum(pip_values, Decimal("0")))
            gross_profit_pips = _money(
                sum((value for value in pip_values if value > 0), Decimal("0"))
            )
            gross_loss_pips = _money(
                sum((value for value in pip_values if value < 0), Decimal("0"))
            )

        return {
            "total_trades": wins + losses + breakeven,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "open_trades": open_trades,
            "cash_pnl": cash,
            "return_percent": self._period_return_percent(user_id, start, cash),
            "net_pips": net_pips,
            "gross_profit_pips": gross_profit_pips,
            "gross_loss_pips": gross_loss_pips,
            "model_500_pnl": model,
            "model_500_return_percent": _pct(model / MODEL_BALANCE * Decimal("100")),
            "source_digest": __import__("hashlib").sha256(
                "|".join(sorted(digests)).encode("utf-8")
            ).hexdigest(),
        }


__all__ = ["CanonicalPerformanceRuntimeService"]
