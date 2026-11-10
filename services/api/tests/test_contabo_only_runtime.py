"""Contabo MT5 is the only production broker executor.
Do not silently reinstate the retired MetaAPI execution path.
"""
from __future__ import annotations

from app.broker_gateway_factory import broker_transport
from app.execution_router_canonical import (
    build_canonical_execution_router,
    build_canonical_pending_reconciler,
    build_management_reliability_runtime,
)
from app.pu_master_mirror_runtime import pu_mirror_effective_mode


def test_stale_remote_broker_env_cannot_select_metaapi(monkeypatch):
    monkeypatch.setenv("SUPER_SIGNALS_BROKER_TRANSPORT", "metaapi")
    assert broker_transport() == "local_bridge"


def test_retired_live_pu_mirror_cannot_be_rearmed(monkeypatch):
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-d9qmcgks728c73a555m0")
    monkeypatch.setenv("SUPER_SIGNALS_PU_MIRROR_MODE", "execute")
    monkeypatch.delenv("SUPER_SIGNALS_PU_MIRROR_EMERGENCY_STOP", raising=False)
    assert pu_mirror_effective_mode() == "off"


def test_legacy_router_and_pending_jobs_remain_disabled(monkeypatch):
    monkeypatch.setenv("SUPER_SIGNALS_DAY28_AUTO_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("SUPER_SIGNALS_BROKER_TRANSPORT", "metaapi")
    factory = lambda: None
    router = build_canonical_execution_router(session_factory=factory)
    assert router is None
    assert build_canonical_pending_reconciler(session_factory=factory, router=router) is None
    assert build_management_reliability_runtime(session_factory=factory, router=router) is None
