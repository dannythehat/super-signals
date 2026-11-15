"""Research-only HTTP wrapper for a *separate* dormant Render web service.

Render free web plans may sleep. A harmless scheduled GET /health wakes the
service, while the research task runs in its own subprocess. No live trading
service is imported, started, restarted, or redeployed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI

logger = logging.getLogger(__name__)
state = {
    "last_started_utc": None,
    "last_finished_utc": None,
    "last_exit_code": None,
    "running": False,
    "runs": 0,
}
_task: asyncio.Task[None] | None = None


async def _score_loop() -> None:
    while True:
        state["running"] = True
        state["last_started_utc"] = datetime.now(UTC).isoformat()
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m", "app.provider_scoring_isolated",
                "--limit", os.environ.get("PROVIDER_RESEARCH_BATCH_LIMIT", "24"),
                "--concurrency", "2",
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=480)
                state["last_exit_code"] = proc.returncode
            except TimeoutError:
                logger.error("MONTHLY_SCORING_RUN_TIMEOUT research child stopped")
                proc.terminate()
                with suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=10)
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                state["last_exit_code"] = 124
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.terminate()
                with suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=10)
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            raise
        except Exception as exc:
            logger.error("MONTHLY_SCORING_RUN_ERROR error_type=%s", type(exc).__name__)
            state["last_exit_code"] = 1
        finally:
            state["runs"] += 1
            state["last_finished_utc"] = datetime.now(UTC).isoformat()
            state["running"] = False
        # Frequent enough to backfill, bounded to one research child.
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _task
    _task = asyncio.create_task(_score_loop(), name="independent-provider-score-loop")
    try:
        yield
    finally:
        if _task is not None:
            _task.cancel()
            with suppress(asyncio.CancelledError):
                await _task


app = FastAPI(title="Super Signals Research-Only Provider Scoring", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, object]:
    """Non-sensitive status. No credentials, provider messages or trade data."""
    return {"service": "provider-monthly-research-only", "status": "ok", **state}
