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


def test_dashboard_account_values_cannot_be_virtualised_again() -> None:
    runtime = (ROOT / "services/api/app/dashboard_runtime.py").read_text(encoding="utf-8")

    assert "_post_epoch_realised_cash" not in runtime
    assert "baseline_balance + realised" not in runtime
    assert "balance=float(balance)" not in runtime
    assert "free_margin=float(equity)" not in runtime


def test_api_declares_provider_performance_separately_from_mt5_account_balance() -> None:
    route = (ROOT / "services/api/app/routes/dashboard_day32.py").read_text(encoding="utf-8")

    assert 'performance_basis: str = "selected_provider_broker_ledger"' in route
