"""Activate production incident guards and the 2026-09-04 11:00 Sofia clean restart."""

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
        from app.incident_20260904_asia_owner_cleanup import run_owner_asia_cleanup

        incident._install_reader_hotfix()
        restart.install_execution_hold()
        restart.install_timeline_filter()
        asyncio.run(run_owner_asia_cleanup())

        # The 11:00 reset rows are already committed before broker cleanup begins. Keep
        # future restarts focused on idempotent broker cleanup and avoid rewriting audit
        # metadata while the fixed restart remains active.
        restart._upsert_overrides = lambda _user_ids: None
        asyncio.run(restart.run_connected_account_restart())
    except Exception as exc:
        print(
            f"SUPER_SIGNALS_PRODUCTION_GUARD_ACTIVATION_FATAL={type(exc).__name__}:{str(exc)[:180]}",
            flush=True,
        )
