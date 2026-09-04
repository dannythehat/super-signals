"""Activate the 2026-09-04 Asia incident guard only in the production uvicorn process."""

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

        incident._install_reader_hotfix()
        asyncio.run(incident._cleanup_stale_asia_exposure())
    except Exception as exc:
        print(
            f"SUPER_SIGNALS_ASIA_INCIDENT_ACTIVATION_FATAL={type(exc).__name__}:{str(exc)[:180]}",
            flush=True,
        )
