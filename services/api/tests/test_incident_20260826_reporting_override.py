import inspect
from decimal import Decimal

from app import dashboard_today_summary, performance_runtime
from app.reporting_overrides import (
    OUTCOME_NOT_OVERRIDDEN_SQL,
    current_day_override,
    override_cash_for_window,
)


def test_override_excludes_only_reporting_outcomes_not_broker_evidence() -> None:
    assert "performance_reporting_overrides" in OUTCOME_NOT_OVERRIDDEN_SQL
    assert "performance_trade_outcomes" not in OUTCOME_NOT_OVERRIDDEN_SQL
    assert "broker_deals" not in OUTCOME_NOT_OVERRIDDEN_SQL
    assert "closed_at" in OUTCOME_NOT_OVERRIDDEN_SQL


def test_today_summary_uses_reviewed_cash_override() -> None:
    source = inspect.getsource(dashboard_today_summary.TodayTradingSummaryService.read)
    assert "current_day_override" in source
    assert "reporting_override[0]" in source


def test_every_runtime_window_excludes_incident_day_and_adds_reviewed_cash() -> None:
    source = inspect.getsource(performance_runtime.CanonicalPerformanceRuntimeService._signal_window)
    assert "OUTCOME_NOT_OVERRIDDEN_SQL" in source
    assert "override_cash_for_window" in source
    assert "realised_cash = reviewed_cash" in source


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _MappingResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def mappings(self) -> "_MappingResult":
        return self

    def first(self) -> object:
        return self._value


class _Session:
    def __init__(self, result: object) -> None:
        self._result = result

    def execute(self, *_args: object, **_kwargs: object) -> object:
        return self._result


def test_override_cash_is_decimal_and_exact() -> None:
    from datetime import UTC, datetime
    from uuid import UUID

    result = override_cash_for_window(
        _Session(_ScalarResult("35.00")),  # type: ignore[arg-type]
        UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d"),
        start=datetime(2026, 8, 25, 21, tzinfo=UTC),
        end=datetime(2026, 8, 26, 15, tzinfo=UTC),
    )
    assert result == Decimal("35.00")


def test_current_day_override_returns_amount_and_reason() -> None:
    from datetime import UTC, datetime
    from uuid import UUID

    result = current_day_override(
        _Session(_MappingResult({"realised_cash_pnl": "35.00", "reason": "incident"})),  # type: ignore[arg-type]
        UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d"),
        day_start=datetime(2026, 8, 25, 21, tzinfo=UTC),
    )
    assert result == (Decimal("35.00"), "incident")
