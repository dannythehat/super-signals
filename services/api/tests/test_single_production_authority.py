from __future__ import annotations

import inspect
from decimal import Decimal

import app.main as main
import app.production_listener as production
from app.provider_risk_policy import (
    POLICY_GENERATION,
    provider_risk_profile,
    provider_side_enabled,
    provider_tp_limit,
)


def test_main_has_one_production_listener_entrypoint() -> None:
    source = inspect.getsource(main)
    assert main.build_production_listener_manager.__module__ == "app.production_listener"
    assert "from app.production_listener import build_production_listener_manager" in source
    assert "build_day28_listener_manager" not in source
    assert "build_day38_listener_manager" not in source
    assert production.PRODUCTION_LISTENER_GENERATION == "canonical-v1"


def test_current_owner_provider_policy_cannot_regress_to_old_tig_veto() -> None:
    assert POLICY_GENERATION == "owner-authority-2026-09-09-v1"
    source = "TIG’s Asia Trades"
    for side in ("BUY", "SELL"):
        assert provider_side_enabled(source_name=source, side=side)
        assert provider_tp_limit(source_name=source, side=side) is None
        assert provider_risk_profile(
            source_name=source,
            side=side,
            position_count=4,
        ) == (Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1"))


def test_current_fx_policy_is_identical_for_buy_and_sell() -> None:
    source = "FXTradingVision l Forex & Crypto Signals 🚀"
    expected = (Decimal("5"), Decimal("5"), Decimal("1"))
    for side in ("BUY", "SELL"):
        assert provider_side_enabled(source_name=source, side=side)
        assert provider_tp_limit(source_name=source, side=side) == 3
        assert provider_risk_profile(
            source_name=source,
            side=side,
            position_count=3,
        ) == expected
