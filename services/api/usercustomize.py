"""Activate the authoritative 4 Sep 2026 11:00 Sofia clean-restart guards."""

from __future__ import annotations

import asyncio
import os
import sys


def _should_activate() -> bool:
    argv = " ".join(sys.argv).lower()
    return (
        os.getenv("SUPER_SIGNALS_ASIA_INCIDENT_20260904", "").strip() == "1"
        and "/app/services/api" in os.getenv("PYTHONPATH", "")
        and "uvicorn" in argv
    )


if _should_activate():
    try:
        import sitecustomize as incident
        import app.restart_20260904_1100 as restart
        from app.restart_dashboard_patch_20260904 import install_restart_dashboard_patch
        from app.restart_repair_20260904 import restore_restart_overrides

        # Keep only the provider-language compatibility shim from the morning incident.
        # All legacy broker cleanup/reporting mutation is retired after the 11:00 reset.
        incident._install_reader_hotfix()

        # Reassert the clean restart as the sole Sep-4 reporting boundary before any
        # dashboard service is instantiated. Owner intentionally retains the -$3.60
        # cleanup settlement; other connected users retain a zero boundary carry.
        restore_restart_overrides()
        restart.install_execution_hold()
        restart.install_timeline_filter()
        install_restart_dashboard_patch()

        # Broker cleanup remains idempotent and targets only pre-11 positions. The
        # authoritative rows were restored above, so skip the old audit-writing upsert.
        restart._upsert_overrides = lambda _user_ids: None
        asyncio.run(restart.run_connected_account_restart())
    except Exception as exc:
        print(
            f"SUPER_SIGNALS_PRODUCTION_GUARD_ACTIVATION_FATAL={type(exc).__name__}:{str(exc)[:180]}",
            flush=True,
        )
