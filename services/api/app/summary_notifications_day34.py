"""Day 34 scheduled performance summaries.

The Owner-approved reporting clock is Europe/Sofia: daily at 21:00, weekly on
Friday at 21:00, and monthly on the first day at 08:00 for the complete previous
calendar month. The implementation lives in scheduled_performance_reports_day34.
"""

from app.scheduled_performance_reports_day34 import (
    Day34ScheduledPerformanceReportService,
    SummarySeedResult,
)


class Day34SummaryNotificationService(Day34ScheduledPerformanceReportService):
    """Compatibility name used by the existing Day 34 Telegram publisher."""


__all__ = ["Day34SummaryNotificationService", "SummarySeedResult"]
