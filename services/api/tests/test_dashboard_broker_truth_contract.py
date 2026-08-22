from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_dashboard_and_performance_use_bounded_resilient_metaapi_reads() -> None:
    dashboard_route = (
        ROOT / "services/api/app/routes/dashboard_day32.py"
    ).read_text(encoding="utf-8")
    performance_route = (
        ROOT / "services/api/app/routes/performance_day33.py"
    ).read_text(encoding="utf-8")

    assert "gateway=ResilientMetaApiReadGateway()" in dashboard_route
    assert "gateway=ResilientMetaApiReadGateway()" in performance_route
    assert "gateway=MetaApiReadGateway()" not in dashboard_route
    assert "gateway=MetaApiReadGateway()" not in performance_route


def test_dashboard_accounting_keeps_live_broker_truth_and_demo_reset_isolation() -> None:
    runtime = (ROOT / "services/api/app/dashboard_runtime.py").read_text(encoding="utf-8")
    accounting = (ROOT / "services/api/app/trading_accounting.py").read_text(encoding="utf-8")

    assert "_post_epoch_realised_cash" not in runtime
    assert "free_margin=float(equity)" not in runtime
    assert "live account balance remains the actual broker balance" in runtime.lower()
    assert "DEAL_TYPE_BALANCE" in accounting
    assert "capital movements" in accounting.lower()
    assert "displayed_balance" in accounting


def test_api_declares_canonical_user_trading_ledger() -> None:
    route = (ROOT / "services/api/app/routes/dashboard_day32.py").read_text(encoding="utf-8")

    assert 'performance_basis: str = "canonical_user_trading_ledger"' in route
    assert "performance_timezone" in route
    assert "daily_profit" in route
