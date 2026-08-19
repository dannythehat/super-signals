from __future__ import annotations

from app.production_listener import PRODUCTION_LISTENER_GENERATION
from app.telegram_listener_canonical import CanonicalProductionTelegramListenerManager


def test_production_listener_is_canonical_generation() -> None:
    assert PRODUCTION_LISTENER_GENERATION == "canonical-v1"


def test_canonical_listener_owns_ingress_and_exact_revision_processing() -> None:
    # These methods must be defined by the production class itself.  If they fall
    # back to a day-numbered base class, an old implementation can silently return.
    assert "_persist_message" in CanonicalProductionTelegramListenerManager.__dict__
    assert "_persist_edit" in CanonicalProductionTelegramListenerManager.__dict__
    assert "_exact_saved_revision_index" in CanonicalProductionTelegramListenerManager.__dict__
    assert "_process_saved_edit" in CanonicalProductionTelegramListenerManager.__dict__


def test_deleted_telegram_patch_modules_are_not_package_startup_dependencies() -> None:
    import app

    package_source = app.__loader__.get_source("app") if app.__loader__ is not None else ""
    assert "telegram_fast_ingress" not in (package_source or "")
    assert "telegram_revision_serialization" not in (package_source or "")
