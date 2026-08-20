from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_tdc_is_permanently_revoked_by_migration() -> None:
    migration = (
        ROOT
        / "services/api/migrations/versions/0028_retire_tdc_provider.py"
    ).read_text(encoding="utf-8")
    assert "-1004415242875" in migration
    assert "status='revoked'" in migration
    assert "downgrade()" in migration
    assert "pass" in migration


def test_tdc_cleanup_removes_broker_exposure_before_local_trade_artifacts() -> None:
    cleanup = (
        ROOT / "services/api/app/retire_tdc_provider_once.py"
    ).read_text(encoding="utf-8")
    assert "read_positions" in cleanup
    assert "read_orders" in cleanup
    assert "read_history_orders_by_time_range" in cleanup
    assert "close_position" in cleanup
    assert "cancel_order" in cleanup
    assert cleanup.index("_remove_broker_exposure") < cleanup.index("_purge_trading_artifacts")


def test_tdc_cleanup_removes_all_user_facing_performance_artifacts() -> None:
    cleanup = (
        ROOT / "services/api/app/retire_tdc_provider_once.py"
    ).read_text(encoding="utf-8")
    for required in (
        "DELETE FROM notification_events",
        "DELETE FROM telegram_publications",
        "DELETE FROM performance_trade_outcomes",
        "DELETE FROM positions",
        "DELETE FROM signals",
        "DELETE FROM performance_summaries",
        "rebuild_summaries",
    ):
        assert required in cleanup


def test_tdc_cleanup_is_wired_only_as_controlled_startup_maintenance() -> None:
    start = (ROOT / "scripts/render-start.sh").read_text(encoding="utf-8")
    assert "python -m app.retire_tdc_provider_once" in start
    assert start.index("alembic -c alembic.ini upgrade head") < start.index(
        "python -m app.retire_tdc_provider_once"
    )
    assert start.index("python -m app.retire_tdc_provider_once") < start.index(
        "python -m app.bootstrap"
    )
