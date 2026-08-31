"""Production performance view over the canonical broker-account ledger.

Accounting contract:
* one provider Signal is one trade, regardless of TP tranche count;
* only fully broker-decided Signals are Won/Lost/BE headline trades;
* any ``closed_unknown`` leg is settlement diagnostics, never a completed trade;
* cash/P&L and pips come only from broker-decided legs;
* revoked providers are outside the user-facing performance universe;
* open/pending state is reported separately;
* Today is the configured local calendar day (Europe/Sofia by default), never UTC
  midnight unless explicitly configured that way;
* the Owner paper run has one immutable balance origin. Pre-origin evidence remains in
  the broker/audit ledger, while any position that was genuinely still open at the origin
  contributes the part of its result realised after the origin. This keeps the opening
  balance and subsequent broker exits mathematically consistent;
* Today, 7d, 30d, Month and All-time are ordinary cumulative views. No calendar day,
  deploy or restart may manufacture zero windows or reset post-origin results.

The underlying account/deal synchronisation remains CanonicalPerformanceLedgerService.
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
from app.paper_run_epoch import PAPER_RUN_STARTED_AT, active_paper_epoch
from app.performance_ledger_canonical import CanonicalPerformanceLedgerService
from app.performance_ledger_day33 import (
    MODEL_BALANCE,
    Day33PerformanceWindow,
    _d,
    _money,
    _pct,
)
from app.reporting_overrides import (
    OUTCOME_NOT_OVERRIDDEN_SQL,
    override_cash_for_window,
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

    @staticmethod
    def _later(left: datetime, right: datetime) -> datetime:
        if left.tzinfo is None:
            left = left.replace(tzinfo=UTC)
        if right.tzinfo is None:
            right = right.replace(tzinfo=UTC)
        return max(left.astimezone(UTC), right.astimezone(UTC))

    def _run_start(self, user_id: UUID) -> datetime | None:
        epoch = active_paper_epoch(user_id)
        return epoch.started_at if epoch is not None else None

    def _eligible_signal_ids(self, user_id: UUID) -> set[UUID] | None:
        """Signals visible in the active run, including genuine carry-over positions.

        A signal that began before the balance origin is still relevant when one of its
        outcomes closes after the origin, or while it remains open/pending. Pre-origin
        closed outcomes remain hidden; only their already-realised cash is represented in
        the accepted opening balance.
        """
        run_start = self._run_start(user_id)
        if run_start is None:
            return None
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT DISTINCT s.id
                    FROM signals AS s
                    JOIN sources AS src ON src.id=s.source_id
                    WHERE src.status<>'revoked'
                      AND (
                            COALESCE(s.source_posted_at,s.created_at)>=:run_started_at
                            OR EXISTS (
                                SELECT 1
                                FROM performance_trade_outcomes AS o
                                WHERE o.user_id=:user_id
                                  AND o.signal_id=s.id
                                  AND (
                                        (
                                            o.status IN ('won','lost','breakeven')
                                            AND o.closed_at>=:run_started_at
                                        )
                                        OR o.status IN ('open','pending')
                                        OR (
                                            o.status='closed_unknown'
                                            AND COALESCE(o.closed_at,o.derived_at)>=:run_started_at
                                        )
                                  )
                            )
                      )
                    """
                ),
                {"run_started_at": run_start, "user_id": user_id},
            ).scalars().all()
        return {UUID(str(value)) for value in values}

    def _period_return_percent(
        self,
        user_id: UUID,
        period_start: datetime,
        cash_pnl: Decimal,
    ) -> Decimal | None:
        epoch = active_paper_epoch(user_id)
        if epoch is not None:
            if epoch.baseline_balance <= 0:
                return None
            return _pct(cash_pnl / epoch.baseline_balance * Decimal("100"))
        return super()._period_return_percent(user_id, period_start, cash_pnl)

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
        run_start = self._run_start(user_id)

        def bounded(value: datetime) -> datetime:
            return self._later(value, run_start) if run_start is not None else value

        windows = (
            ("today", "Today", bounded(start_today)),
            ("7d", "7 days", bounded(point - timedelta(days=7))),
            ("30d", "30 days", bounded(point - timedelta(days=30))),
            ("month", "Month", bounded(start_month)),
            ("all", "All time", run_start),
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
        clauses = [
            "o.user_id=:user_id",
            "src.status<>'revoked'",
            OUTCOME_NOT_OVERRIDDEN_SQL,
        ]
        params: dict[str, Any] = {"user_id": user_id, "window_end": now}
        run_start = self._run_start(user_id)
        if run_start is not None:
            clauses.append(
                "(COALESCE(s.source_posted_at,s.created_at)>=:run_started_at "
                "OR (o.status IN ('won','lost','breakeven') AND o.closed_at>=:run_started_at) "
                "OR o.status IN ('open','pending') "
                "OR (o.status='closed_unknown' AND COALESCE(o.closed_at,o.derived_at)>=:run_started_at))"
            )
            params["run_started_at"] = run_start
        if since is not None:
            clauses.append(
                "(o.closed_at>=:since OR o.status IN ('open','pending') OR "
                "(o.status='closed_unknown' AND COALESCE(o.closed_at,o.derived_at)>=:since))"
            )
            params["since"] = since
        clauses.append("COALESCE(o.closed_at,o.opened_at,o.derived_at)<:window_end")

        override_start = since or datetime(1970, 1, 1, tzinfo=UTC)
        if run_start is not None:
            override_start = self._later(override_start, run_start)

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
                    JOIN signals AS s ON s.id=o.signal_id
                    JOIN sources AS src ON src.id=s.source_id
                    WHERE {' AND '.join(clauses)}
                    GROUP BY o.signal_id
                    """
                ),
                params,
            ).mappings().all()
            reviewed_cash = override_cash_for_window(
                session,
                user_id,
                start=override_start,
                end=now,
            )

        wins = losses = breakeven = 0
        open_trades = 0
        realised_cash = reviewed_cash
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

    def _timeline_rows(self, user_id: UUID) -> list[Any]:
        rows = [dict(row) for row in super()._timeline_rows(user_id)]
        eligible = self._eligible_signal_ids(user_id)
        if eligible is None:
            with self._session_factory() as session:
                values = session.execute(
                    text(
                        """
                        SELECT s.id
                        FROM signals s
                        JOIN sources src ON src.id=s.source_id
                        WHERE src.status<>'revoked'
                        """
                    )
                ).scalars().all()
            eligible = {UUID(str(value)) for value in values}
        rows = [row for row in rows if UUID(str(row["signal_id"])) in eligible]

        run_start = self._run_start(user_id)
        if run_start is None or not rows:
            return rows

        signal_ids = [UUID(str(row["signal_id"])) for row in rows]
        with self._session_factory() as session:
            carry = {
                UUID(str(row["signal_id"])): row
                for row in session.execute(
                    text(
                        """
                        SELECT
                            o.signal_id,
                            COUNT(*) FILTER (
                                WHERE o.status IN ('won','lost','breakeven')
                                  AND o.closed_at>=:run_started_at
                            )::int AS closed_positions,
                            COUNT(*) FILTER (WHERE o.status='open')::int AS open_positions,
                            COUNT(*) FILTER (WHERE o.status='pending')::int AS pending_positions,
                            COALESCE(SUM(o.cash_pnl) FILTER (
                                WHERE o.status IN ('won','lost','breakeven')
                                  AND o.closed_at>=:run_started_at
                            ),0) AS cash_pnl,
                            CASE
                                WHEN COUNT(*) FILTER (
                                    WHERE o.status IN ('won','lost','breakeven')
                                      AND o.closed_at>=:run_started_at
                                      AND o.net_pips IS NULL
                                )=0
                                THEN SUM(o.net_pips) FILTER (
                                    WHERE o.status IN ('won','lost','breakeven')
                                      AND o.closed_at>=:run_started_at
                                )
                                ELSE NULL
                            END AS net_pips,
                            SUM(o.model_500_pnl) FILTER (
                                WHERE o.status IN ('won','lost','breakeven')
                                  AND o.closed_at>=:run_started_at
                            ) AS model_500_pnl,
                            MAX(o.closed_at) FILTER (
                                WHERE o.status IN ('won','lost','breakeven')
                                  AND o.closed_at>=:run_started_at
                            ) AS closed_at,
                            BOOL_OR(o.status='won' AND o.closed_at>=:run_started_at) AS has_win,
                            BOOL_OR(o.status='lost' AND o.closed_at>=:run_started_at) AS has_loss,
                            BOOL_OR(o.status='breakeven' AND o.closed_at>=:run_started_at) AS has_breakeven,
                            BOOL_OR(
                                o.status='closed_unknown'
                                AND COALESCE(o.closed_at,o.derived_at)>=:run_started_at
                            ) AS has_unknown
                        FROM performance_trade_outcomes AS o
                        JOIN signals AS s ON s.id=o.signal_id
                        WHERE o.user_id=:user_id
                          AND o.signal_id=ANY(:signal_ids)
                          AND COALESCE(s.source_posted_at,s.created_at)<:run_started_at
                        GROUP BY o.signal_id
                        """
                    ),
                    {
                        "user_id": user_id,
                        "signal_ids": signal_ids,
                        "run_started_at": run_start,
                    },
                ).mappings().all()
            }

        for row in rows:
            signal_id = UUID(str(row["signal_id"]))
            sliced = carry.get(signal_id)
            if sliced is None:
                continue
            closed_positions = int(sliced["closed_positions"] or 0)
            open_positions = int(sliced["open_positions"] or 0)
            pending_positions = int(sliced["pending_positions"] or 0)
            if not (closed_positions or open_positions or pending_positions or bool(sliced["has_unknown"])):
                continue
            row["closed_positions"] = closed_positions
            row["open_positions"] = open_positions
            row["pending_positions"] = pending_positions
            row["position_count"] = closed_positions + open_positions + pending_positions
            row["cash_pnl"] = sliced["cash_pnl"]
            row["net_pips"] = sliced["net_pips"]
            row["model_500_pnl"] = sliced["model_500_pnl"]
            row["closed_at"] = sliced["closed_at"] or row.get("closed_at")
            row["has_win"] = bool(sliced["has_win"])
            row["has_loss"] = bool(sliced["has_loss"])
            row["has_breakeven"] = bool(sliced["has_breakeven"])
            row["has_unknown"] = bool(sliced["has_unknown"])
        return rows

    def read_shared_live_board(self) -> list[Any]:
        rows = list(super().read_shared_live_board())
        if not rows:
            return rows
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT DISTINCT s.id
                    FROM signals s
                    JOIN sources src ON src.id=s.source_id
                    WHERE src.status<>'revoked'
                      AND (
                            COALESCE(s.source_posted_at,s.created_at)>=:run_started_at
                            OR EXISTS (
                                SELECT 1 FROM positions p
                                WHERE p.signal_id=s.id
                                  AND p.status IN ('open','pending')
                            )
                      )
                    """
                ),
                {"run_started_at": PAPER_RUN_STARTED_AT},
            ).scalars().all()
        eligible = {UUID(str(value)) for value in values}
        return [row for row in rows if UUID(str(row["signal_id"])) in eligible]

    def _summary_metrics(
        self,
        user_id: UUID,
        rows: list[Any],
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Build persisted summaries as one Signal = one trade."""
        run_start = self._run_start(user_id)
        eligible = self._eligible_signal_ids(user_id)
        if eligible is not None:
            rows = [row for row in rows if UUID(str(row["signal_id"])) in eligible]
        if run_start is not None:
            start = self._later(start, run_start)

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
