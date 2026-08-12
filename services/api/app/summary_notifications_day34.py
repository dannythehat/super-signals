"""Day 34 completed-period shared summaries derived from the Day 33 ledger.

The service never recalculates real performance from Telegram text. It publishes only
completed Day 33 portfolio summary rows and exposes only the standardized $500 model,
never the reference user's real cash P&L, balance or account identifiers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

_PERIOD_LABELS = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "monthly": "MONTHLY",
}


@dataclass(frozen=True, slots=True)
class SummarySeedResult:
    created: int
    checked_at: datetime
    broker_trade_action_created: bool = False


class Day34SummaryNotificationService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        reference_user_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._reference_user_id = reference_user_id

    def seed_due(self, *, now: datetime | None = None) -> SummarySeedResult:
        point = (now or datetime.now(UTC)).astimezone(UTC)
        with self._session_factory() as session:
            state = session.execute(
                text(
                    """
                    SELECT publish_after, last_seeded_period_end
                    FROM day34_summary_state
                    WHERE id=1
                    """
                )
            ).mappings().one()
            publish_after = state["publish_after"]

            rows = session.execute(
                text(
                    """
                    SELECT
                        id, period_type, period_start, period_end,
                        total_trades, wins, losses, breakeven, open_trades,
                        net_pips, model_500_pnl, model_500_return_percent,
                        source_digest
                    FROM performance_summaries
                    WHERE user_id=:user_id
                      AND dimension_type='portfolio'
                      AND dimension_key='all'
                      AND period_type IN ('daily','weekly','monthly')
                      AND period_end > :publish_after
                      AND period_end <= :now
                    ORDER BY period_end, period_type
                    """
                ),
                {
                    "user_id": self._reference_user_id,
                    "publish_after": publish_after,
                    "now": point,
                },
            ).mappings().all()

            created = 0
            last_period_end = state["last_seeded_period_end"]
            for row in rows:
                title, body = self._render(row)
                event_key = self._event_key(row)
                inserted = session.execute(
                    text(
                        """
                        INSERT INTO notification_events (
                            event_key, signal_id, lifecycle_event_id, user_id,
                            audience, kind, title, body, payload
                        ) VALUES (
                            :event_key, NULL, NULL, NULL,
                            'shared', :kind, :title, :body,
                            CAST(:payload AS jsonb)
                        )
                        ON CONFLICT (event_key) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "event_key": event_key,
                        "kind": f"summary_{row['period_type']}",
                        "title": title,
                        "body": body,
                        "payload": json.dumps(
                            {
                                "day34_summary": True,
                                "ledger_summary_id": str(row["id"]),
                                "period_type": str(row["period_type"]),
                                "period_start": row["period_start"].isoformat(),
                                "period_end": row["period_end"].isoformat(),
                                "source_digest": str(row["source_digest"]),
                                "closed_positions": int(row["total_trades"]),
                                "wins": int(row["wins"]),
                                "losses": int(row["losses"]),
                                "breakeven": int(row["breakeven"]),
                                "open_positions": int(row["open_trades"]),
                                "net_pips": self._plain(row["net_pips"]),
                                "model_balance": "500",
                                "recommended_risk_percent": "1",
                                "model_500_pnl": self._plain(row["model_500_pnl"]),
                                "model_500_return_percent": self._plain(
                                    row["model_500_return_percent"]
                                ),
                                "real_user_balance_exposed": False,
                                "real_user_pnl_exposed": False,
                                "provider_identity_exposed": False,
                                "broker_trade_action_created": False,
                            }
                        ),
                    },
                ).scalar_one_or_none()
                if inserted is not None:
                    created += 1
                if last_period_end is None or row["period_end"] > last_period_end:
                    last_period_end = row["period_end"]

            session.execute(
                text(
                    """
                    UPDATE day34_summary_state
                    SET last_seeded_period_end=COALESCE(:last_period_end,last_seeded_period_end),
                        last_checked_at=:now,
                        updated_at=:now
                    WHERE id=1
                    """
                ),
                {"last_period_end": last_period_end, "now": point},
            )
            session.commit()
            return SummarySeedResult(created=created, checked_at=point)

    @staticmethod
    def _event_key(row: Any) -> str:
        start = row["period_start"].astimezone(UTC).isoformat()
        return f"summary:{row['period_type']}:{start}"

    @staticmethod
    def _render(row: Any) -> tuple[str, str]:
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

    @staticmethod
    def _plain(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        return str(value)


__all__ = ["Day34SummaryNotificationService", "SummarySeedResult"]
