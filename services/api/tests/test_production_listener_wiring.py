from __future__ import annotations

import inspect

import pytest


def test_production_main_uses_only_canonical_listener_entrypoint() -> None:
    import app.main as main

    assert main.build_production_listener_manager.__module__ == "app.production_listener"
    assert not hasattr(main, "build_day28_listener_manager")
    assert not hasattr(main, "build_day38_listener_manager")


def test_canonical_listener_generation_and_router_are_mandatory(monkeypatch) -> None:
    import app.production_listener as production

    assert production.PRODUCTION_LISTENER_GENERATION == "canonical-v1"

    class WrongManager:
        pass

    monkeypatch.setattr(
        production,
        "build_canonical_production_listener_manager",
        lambda **_: WrongManager(),
    )
    with pytest.raises(RuntimeError, match="production_listener_generation_mismatch"):
        production.build_production_listener_manager()


def test_canonical_manager_without_router_is_rejected(monkeypatch) -> None:
    import app.production_listener as production

    manager = object.__new__(production.CanonicalProductionTelegramListenerManager)
    manager._canonical_router = None
    monkeypatch.setattr(
        production,
        "build_canonical_production_listener_manager",
        lambda **_: manager,
    )
    with pytest.raises(RuntimeError, match="production_execution_router_missing"):
        production.build_production_listener_manager()


def test_main_source_has_no_day_numbered_listener_import() -> None:
    import app.main as main

    source = inspect.getsource(main)
    assert "telegram_listener_day28" not in source
    assert "telegram_listener_day38" not in source
    assert "from app.production_listener import build_production_listener_manager" in source
