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
        from app.incident_20260904_asia_owner_cleanup import run_owner_asia_cleanup
        from app.restart_20260904_1100 import (
            install_execution_hold,
            install_timeline_filter,
            run_connected_account_restart,
        )

        incident._install_reader_hotfix()
        install_execution_hold()
        install_timeline_filter()
        asyncio.run(run_owner_asia_cleanup())
        asyncio.run(run_connected_account_restart())
    except Exception as exc:
        print(
            f"SUPER_SIGNALS_PRODUCTION_GUARD_ACTIVATION_FATAL={type(exc).__name__}:{str(exc)[:180]}",
            flush=True,
        )
