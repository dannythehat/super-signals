"""Focused Day 32 mobile-dashboard response contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

from app.routes.dashboard_day32 import (
    AccountResponse,
    ConnectionResponse,
    DashboardResponse,
    PerformanceResponse,
    TradingResponse,
    WinLossResponse,
)


def _response() -> DashboardResponse:
    return DashboardResponse(
        connection=ConnectionResponse(
            configured=True,
            status="connected",
            account_environment="demo",
            login_masked="****1234",
            server="VantageInternational-Demo",
            error_code=None,
            read_at=datetime(2026, 8, 12, 11, 30, tzinfo=UTC),
        ),
        account=AccountResponse(
            currency="USD",
            balance=1024.82,
            equity=1024.82,
            margin=0.0,
            free_margin=1024.82,
            trade_allowed=True,
        ),
        trading=TradingResponse(
            available=False,
            status=None,
            risk_percent=None,
            allow_double_lot=None,
            effective_double_lot_risk_percent=None,
        ),
        open_profit=0.0,
        open_positions=(),
        latest_signal=None,
        recent_completed=(),
        performance=(
            PerformanceResponse(
                key="today",
                label="Today",
                amount=None,
                known_position_count=0,
                provisional_until_day33=True,
            ),
        ),
        win_loss=WinLossResponse(
            wins=0,
            losses=0,
            breakeven=0,
            known_results=0,
            win_rate_percent=None,
        ),
        activity=(),
        reconciled_external_positions=3,
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        result = set(value)
        for child in value.values():
            result.update(_all_keys(child))
        return result
    if isinstance(value, (list, tuple)):
        result: set[str] = set()
        for child in value:
            result.update(_all_keys(child))
        return result
    return set()


def test_dashboard_never_exposes_private_provider_or_telegram_identity() -> None:
    payload = _response().model_dump(mode="json")
    forbidden = {
        "source_id",
        "source_title",
        "source_alias",
        "provider",
        "provider_id",
        "provider_name",
        "telegram_chat_id",
        "telegram_message_id",
        "original_text",
        "raw_text",
    }
    assert _all_keys(payload).isdisjoint(forbidden)


def test_dashboard_is_explicitly_read_only_at_broker() -> None:
    response = _response()
    assert response.broker_trade_action_created is False
    assert response.reconciled_external_positions == 3


def test_unverified_performance_is_unknown_not_fake_zero() -> None:
    response = _response()
    period = response.performance[0]
    assert response.canonical_performance_ready is False
    assert period.provisional_until_day33 is True
    assert period.known_position_count == 0
    assert period.amount is None


def test_dashboard_numbers_are_json_numbers_not_decimal_strings() -> None:
    payload = _response().model_dump(mode="json")
    assert payload["account"]["balance"] == 1024.82
    assert isinstance(payload["account"]["balance"], float)
