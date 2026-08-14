"""Day 34 scheduled performance summaries.

The Owner-approved reporting clock is Europe/Sofia: daily at 21:00, weekly on
Friday at 21:00, and monthly on the first day at 08:00 for the complete previous
calendar month. The implementation lives in scheduled_performance_reports_day34.

The legacy one-argument renderer remains available for the existing Day 34
privacy/wording contract tests; live scheduled reports use the new two-argument
renderer path.
"""

from decimal import Decimal
from typing import Any

from app.scheduled_performance_reports_day34 import (
    Day34ScheduledPerformanceReportService,
    ScheduledReportPeriod,
    SummarySeedResult,
)

_PERIOD_LABELS = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "monthly": "MONTHLY",
}


class Day34SummaryNotificationService(Day34ScheduledPerformanceReportService):
    """Compatibility name used by the existing Day 34 Telegram publisher."""

    @staticmethod
    def _render(
        period_or_row: ScheduledReportPeriod | Any,
        metrics: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        if metrics is not None:
            return Day34ScheduledPerformanceReportService._render(period_or_row, metrics)

        row = period_or_row
        period = str(row["period_type"])
        label = _PERIOD_LABELS[period]
        closed = int(row["total_trades"])
        wins = int(row["wins"])
        losses = int(row["losses"])
        breakeven = int(row["breakeven"])
        open_positions = int(row["open_trades"])
        model_pnl = Decimal(str(row["model_500_pnl"] or 0))
        model_return = Decimal(str(row["model_500_return_percent"] or 0))

        title = f"📊 {label} SUPER SIGNALS SUMMARY"
        lines = [
            f"Closed positions: {closed} · Won: {wins} · Lost: {losses} · BE: {breakeven}",
            f"Still open: {open_positions}",
        ]
        if row["net_pips"] is not None:
            lines.append(Day34SummaryNotificationService._signed(row["net_pips"], suffix=" pips"))
        lines.append(
            "$500 example at Recommended 1%: "
            + Day34SummaryNotificationService._signed(model_pnl, prefix="$", money=True)
            + " · "
            + Day34SummaryNotificationService._signed(model_return, suffix="%")
        )
        lines.append("Broker-led results · wins and losses included")
        return title, "\n".join(lines)

    @staticmethod
    def _signed(
        value: Any,
        *,
        prefix: str = "",
        suffix: str = "",
        money: bool = False,
    ) -> str:
        amount = Decimal(str(value))
        body = f"{abs(amount):.2f}" if money else format(abs(amount).normalize(), "f")
        sign = "+" if amount > 0 else "-" if amount < 0 else ""
        return f"{sign}{prefix}{body}{suffix}"


__all__ = ["Day34SummaryNotificationService", "SummarySeedResult"]
