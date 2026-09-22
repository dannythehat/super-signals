from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_dashboard_display_reads_fail_fast_while_performance_stays_resilient() -> None:
    dashboard_route = (
        ROOT / "services/api/app/routes/dashboard_day32.py"
    ).read_text(encoding="utf-8")
    performance_route = (
        ROOT / "services/api/app/routes/performance_day33.py"
    ).read_text(encoding="utf-8")

    # Opening/refreshing the UI must never wait through execution-grade retries.
    assert "ResilientMetaApiReadGateway(timeout_seconds=2.5, attempts=1)" in dashboard_route
    # Deeper performance/broker reconciliation remains bounded and resilient.
    assert "gateway=ResilientMetaApiReadGateway()" in performance_route
    assert "gateway=MetaApiReadGateway()" not in dashboard_route
    assert "gateway=MetaApiReadGateway()" not in performance_route


def test_dashboard_account_values_are_metaapi_truth() -> None:
    runtime = (ROOT / "services/api/app/dashboard_runtime.py").read_text(encoding="utf-8")

    assert "MetaAPI/MT5 truth" in runtime
    assert "displayed_balance" not in runtime
    assert "display_balance" not in runtime
    assert "display_equity" not in runtime
    assert "display_free_margin" not in runtime


def test_api_declares_canonical_user_trading_ledger() -> None:
    route = (ROOT / "services/api/app/routes/dashboard_day32.py").read_text(encoding="utf-8")

    assert 'performance_basis: str = "canonical_user_trading_ledger"' in route
    assert "performance_timezone" in route
    assert "daily_profit" in route
