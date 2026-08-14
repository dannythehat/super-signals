"""Day 34 scheduled performance summaries.

The Owner-approved reporting clock is Europe/Sofia: daily at 21:00, weekly on
Friday at 21:00, and monthly on the first day at 08:00 for the complete previous
calendar month.

Live reports lead with the broker account's net equity change across the reporting
period when both boundary snapshots exist. Realised and floating P/L remain
visible underneath as context. The timezone is intentionally not printed into
member-facing Telegram reports.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.publisher_config import get_publisher_settings
from app.scheduled_performance_reports_day34 import (
    SOFIA,
    Day34ScheduledPerformanceReportService,
    ScheduledReportPeriod,
    SummarySeedResult,
)
from app.telegram_publisher import TelegramPublishError, _bot_api_call

logger = logging.getLogger(__name__)

_PERIOD_LABELS = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "monthly": "MONTHLY",
}


@dataclass(frozen=True, slots=True)
class SentSummaryRepair:
    notification_id: UUID
    delivery_id: UUID
    destination_chat_id: int
    telegram_message_id: int
    title: str
    body: str
    rendered_text: str
    period_net_pnl: Decimal | None


class Day34SummaryNotificationService(Day34ScheduledPerformanceReportService):
    """Scheduled reports plus compatibility and one-time sent-message truth repair."""

    def seed_due(self, *, now: datetime | None = None) -> SummarySeedResult:
        result = super().seed_due(now=now)
        self._repair_sent_messages_safely()
        return result

    def _metrics(
        self,
        session: Any,
        *,
        start: datetime,
        end: datetime,
        report_time: datetime,
    ) -> dict[str, Any]:
        metrics = super()._metrics(
            session,
            start=start,
            end=end,
            report_time=report_time,
        )
        start_snapshot = session.execute(
            text(
                """
                SELECT currency, equity
                FROM performance_account_snapshots
                WHERE user_id=:user_id AND captured_at<=:period_start
                ORDER BY captured_at DESC
                LIMIT 1
                """
            ),
            {"user_id": self._reference_user_id, "period_start": start},
        ).mappings().first()
        end_snapshot = session.execute(
            text(
                """
                SELECT currency, equity
                FROM performance_account_snapshots
                WHERE user_id=:user_id AND captured_at<=:report_time
                ORDER BY captured_at DESC
                LIMIT 1
                """
            ),
            {"user_id": self._reference_user_id, "report_time": report_time},
        ).mappings().first()

        period_net: Decimal | None = None
        if start_snapshot is not None and end_snapshot is not None:
            start_currency = str(start_snapshot["currency"] or "USD").upper()
            end_currency = str(end_snapshot["currency"] or "USD").upper()
            if start_currency == end_currency:
                period_net = Decimal(str(end_snapshot["equity"])) - Decimal(
                    str(start_snapshot["equity"])
                )
        metrics["period_net_pnl"] = period_net
        return metrics

    @staticmethod
    def _render(
        period_or_row: ScheduledReportPeriod | Any,
        metrics: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        if metrics is not None:
            return Day34SummaryNotificationService._render_scheduled(period_or_row, metrics)

        # Legacy contract renderer retained for historical Day 34 tests only.
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
    def _render_scheduled(
        period: ScheduledReportPeriod,
        metrics: dict[str, Any],
    ) -> tuple[str, str]:
        label = period.period_type.upper()
        local_start = period.period_start.astimezone(SOFIA)
        local_end = period.period_end.astimezone(SOFIA)
        currency = str(metrics["currency"] or "USD")

        if period.period_type == "daily":
            period_line = f"Period: {local_start:%d %b %H:%M} → {local_end:%d %b %H:%M}"
        elif period.period_type == "weekly":
            period_line = (
                f"Period: {local_start:%a %d %b %H:%M} → "
                f"{local_end:%a %d %b %H:%M}"
            )
        else:
            period_line = f"Period: {local_start:%d %b %Y} → {local_end:%d %b %Y}"

        period_net = metrics.get("period_net_pnl")
        if period_net is None:
            net_line = "NET P/L: unavailable — no opening broker snapshot"
        else:
            net_line = (
                "NET P/L: "
                + Day34ScheduledPerformanceReportService._money(period_net, currency)
            )

        floating = metrics["floating_cash_pnl"]
        floating_text = (
            Day34ScheduledPerformanceReportService._money(floating, currency)
            if floating is not None
            else "unavailable"
        )
        realised_text = Day34ScheduledPerformanceReportService._money(
            metrics["realised_cash_pnl"], currency
        )

        lines = [
            period_line,
            net_line,
            f"Realised during period: {realised_text}",
            f"Floating at cutoff: {floating_text}",
            (
                f"Closed positions: {metrics['closed_positions']} · "
                f"Won: {metrics['wins']} · Lost: {metrics['losses']} · "
                f"BE: {metrics['breakeven']}"
            ),
            f"Open at cutoff: {metrics['open_positions']}",
            "$500 model @ 1%: "
            + Day34ScheduledPerformanceReportService._money(metrics["model_500_pnl"], "USD"),
            "Broker-derived paper results · open P/L is not counted as realised",
        ]
        return f"📊 {label} SUPER SIGNALS P/L", "\n".join(lines)

    def build_sent_report_repairs(self) -> tuple[SentSummaryRepair, ...]:
        """Prepare one-time edits for already-sent v2 reports using their exact cutoff."""
        repairs: list[SentSummaryRepair] = []
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        n.id AS notification_id,
                        n.kind,
                        n.payload,
                        d.id AS delivery_id,
                        d.destination_chat_id,
                        d.telegram_message_id
                    FROM notification_events n
                    JOIN telegram_notification_deliveries d ON d.notification_id=n.id
                    WHERE n.audience='shared'
                      AND COALESCE(n.payload->>'scheduled_performance_report','false')='true'
                      AND d.status='sent'
                      AND d.destination_chat_id IS NOT NULL
                      AND d.telegram_message_id IS NOT NULL
                      AND COALESCE(n.payload->>'truthful_net_format','false')!='true'
                    ORDER BY n.created_at, n.id
                    """
                )
            ).mappings().all()

            for row in rows:
                payload = row["payload"] if isinstance(row["payload"], dict) else {}
                try:
                    period_start = datetime.fromisoformat(str(payload["period_start"]))
                    period_end = datetime.fromisoformat(str(payload["period_end"]))
                    scheduled_at = datetime.fromisoformat(str(payload["scheduled_at"]))
                    period_type = str(payload["period_type"])
                except (KeyError, TypeError, ValueError):
                    continue

                period = ScheduledReportPeriod(
                    period_type=period_type,
                    period_start=period_start,
                    period_end=period_end,
                    scheduled_at=scheduled_at,
                    trigger_at=scheduled_at,
                )
                metrics = self._metrics(
                    session,
                    start=period_start,
                    end=period_end,
                    report_time=scheduled_at,
                )
                title, body = self._render_scheduled(period, metrics)
                repairs.append(
                    SentSummaryRepair(
                        notification_id=row["notification_id"],
                        delivery_id=row["delivery_id"],
                        destination_chat_id=int(row["destination_chat_id"]),
                        telegram_message_id=int(row["telegram_message_id"]),
                        title=title,
                        body=body,
                        rendered_text=f"{title}\n{body}",
                        period_net_pnl=metrics.get("period_net_pnl"),
                    )
                )
        return tuple(repairs)

    def _repair_sent_messages_safely(self) -> None:
        try:
            settings = get_publisher_settings()
            if not settings.enabled or not settings.bot_token:
                return
            for repair in self.build_sent_report_repairs():
                try:
                    _bot_api_call(
                        settings.bot_token,
                        "editMessageText",
                        {
                            "chat_id": repair.destination_chat_id,
                            "message_id": repair.telegram_message_id,
                            "text": repair.rendered_text,
                            "disable_web_page_preview": "true",
                        },
                    )
                except TelegramPublishError as exc:
                    if "message is not modified" not in exc.reason.lower():
                        raise
                self.record_sent_report_repair(repair)
                logger.info(
                    "Telegram summary corrected in place notification_id=%s message_id=%s",
                    repair.notification_id,
                    repair.telegram_message_id,
                )
        except Exception:
            logger.exception("Telegram summary truth repair failed safely; trading unchanged")

    def record_sent_report_repair(self, repair: SentSummaryRepair) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE notification_events
                    SET title=:title,
                        body=:body,
                        payload=payload || CAST(:payload_patch AS jsonb)
                    WHERE id=:notification_id
                    """
                ),
                {
                    "notification_id": repair.notification_id,
                    "title": repair.title,
                    "body": repair.body,
                    "payload_patch": json.dumps(
                        {
                            "period_net_pnl": (
                                format(repair.period_net_pnl.normalize(), "f")
                                if repair.period_net_pnl is not None
                                else None
                            ),
                            "truthful_net_format": True,
                        }
                    ),
                },
            )
            session.execute(
                text(
                    """
                    UPDATE telegram_notification_deliveries
                    SET rendered_text=:rendered_text, updated_at=now()
                    WHERE id=:delivery_id AND status='sent'
                    """
                ),
                {
                    "delivery_id": repair.delivery_id,
                    "rendered_text": repair.rendered_text,
                },
            )
            session.commit()

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


__all__ = [
    "Day34SummaryNotificationService",
    "SentSummaryRepair",
    "SummarySeedResult",
]
