"""Pure-code Day 34 acceptance probe.

This probe performs no database writes, Telegram calls, OpenAI calls or broker actions.
It pins the critical Day 34 safety contracts that can be proven without live integration:
broker-active lifecycle targeting, ambiguity blocking, provider-pips normalization, the
provider-hidden Live Trades Board and standardized $500 summary wording.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.ai_lifecycle_bridge import AiLifecycleBridge
from app.provider_pips_day34 import normalize_provider_pips
from app.summary_notifications_day34 import Day34SummaryNotificationService
from app.telegram_publisher_day34 import Day34TelegramPublisherManager


class _MappingsResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> "_MappingsResult":
        return self

    def all(self) -> list[dict[str, object]]:
        return list(self._rows)


class _ActiveResolverSession:
    """Minimal fake session for the Day 34 broker-active candidate query only."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls = 0

    def execute(self, statement: object, params: object | None = None) -> _MappingsResult:
        sql = str(statement)
        if "SELECT DISTINCT" not in sql or "JOIN positions AS p" not in sql:
            raise AssertionError("day34_code_probe_unexpected_sql")
        self.calls += 1
        return _MappingsResult(self._rows)


def _candidate(signal_id: int, provider_message_id: int) -> dict[str, object]:
    return {
        "id": UUID(int=signal_id),
        "symbol": "XAUUSD",
        "provider_message_id": provider_message_id,
        "source_posted_at": datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    }


def run_day34_code_acceptance_probe() -> None:
    row = {
        "message_id": UUID(int=900),
        "source_id": UUID(int=901),
        "telegram_message_id": 12345,
        "raw_text": "BE now",
        "raw_payload": {},
        "occurred_at": datetime(2026, 8, 12, 12, 5, tzinfo=UTC),
    }

    unique_session = _ActiveResolverSession([_candidate(1, 1001)])
    linked, reason = AiLifecycleBridge._resolve_signal(  # noqa: SLF001 - acceptance probe
        unique_session,  # type: ignore[arg-type]
        row,
        revision_index=0,
    )
    assert linked is not None
    assert linked["id"] == UUID(int=1)
    assert reason == "active_broker_unique"
    assert unique_session.calls == 1

    ambiguous_session = _ActiveResolverSession(
        [_candidate(1, 1001), _candidate(2, 1002)]
    )
    linked, reason = AiLifecycleBridge._resolve_signal(  # noqa: SLF001 - acceptance probe
        ambiguous_session,  # type: ignore[arg-type]
        row,
        revision_index=0,
    )
    assert linked is None
    assert reason == "active_trade_target_ambiguous"
    assert ambiguous_session.calls == 1

    assert normalize_provider_pips("+35 pips") == Decimal("35")
    assert normalize_provider_pips("-12.5 pips") == Decimal("-12.5")
    assert normalize_provider_pips(Decimal("7.25")) == Decimal("7.25")
    assert normalize_provider_pips("around 35 pips maybe") is None

    board = Day34TelegramPublisherManager._render_live_board([])
    assert board == "📌 SUPER SIGNALS · LIVE TRADES\nOPEN 0 · PENDING 0\n\nNo active trades."
    assert "provider" not in board.lower()
    assert "balance" not in board.lower()
    assert "account" not in board.lower()

    title, summary = Day34SummaryNotificationService._render(
        {
            "period_type": "daily",
            "period_start": datetime(2026, 8, 12, tzinfo=UTC),
            "period_end": datetime(2026, 8, 13, tzinfo=UTC),
            "total_trades": 3,
            "wins": 2,
            "losses": 1,
            "breakeven": 0,
            "open_trades": 0,
            "net_pips": Decimal("84"),
            "model_500_pnl": Decimal("7.60"),
            "model_500_return_percent": Decimal("1.52"),
        }
    )
    assert title == "📊 DAILY SUPER SIGNALS SUMMARY"
    assert "Won: 2 · Lost: 1" in summary
    assert "+84 pips" in summary
    assert "$500 example at Recommended 1%: +$7.60 · +1.52%" in summary

    push_public = bool(os.getenv("SUPER_SIGNALS_WEB_PUSH_VAPID_PUBLIC_KEY", "").strip())
    push_private = bool(os.getenv("SUPER_SIGNALS_WEB_PUSH_VAPID_PRIVATE_KEY", "").strip())
    push_subject = bool(os.getenv("SUPER_SIGNALS_WEB_PUSH_VAPID_SUBJECT", "").strip())

    print(
        "Day 34 code acceptance PASSED: active_unique=1 ambiguous_blocked=1 "
        "pips_normalized=1 board_privacy=1 model500_summary=1 "
        f"push_public={int(push_public)} push_private={int(push_private)} "
        f"push_subject={int(push_subject)}"
    )


__all__ = ["run_day34_code_acceptance_probe"]
