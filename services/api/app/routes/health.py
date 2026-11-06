"""Service health route."""

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.db import get_session_factory
from sqlalchemy import text

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str
    components: dict[str, Any]


def component_health() -> dict[str, Any]:
    """Database facts only: never enqueue work or contact a trading terminal."""
    now = datetime.now(UTC)
    profile = os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals")
    local = os.getenv("SUPER_SIGNALS_BROKER_TRANSPORT", "metaapi") == "local_bridge"
    try:
        with get_session_factory()() as session:
            if local:
                # Actual deployed schema has no account_id on local_bridge_workers.
                from app.routes.server_monitor import worker_snapshots
                workers=session.execute(text("SELECT profile,worker_id,last_seen_at FROM local_bridge_workers WHERE profile=:profile"),{"profile":profile}).mappings().all()
                rows=session.execute(text("""SELECT profile,account_id,claimed_by,operation,status,result,created_at,completed_at
                    FROM local_bridge_commands WHERE profile=:profile
                      AND status IN ('succeeded','failed','ambiguous')
                      AND created_at>=NOW()-INTERVAL '5 minutes'
                      AND operation IN ('read_account_information','read_positions')
                    ORDER BY created_at DESC LIMIT 1200"""),{"profile":profile}).mappings().all()
                observed=worker_snapshots(rows,workers,now)
                states=[r["broker_state"] for r in observed]
                status="unavailable" if not states else "fresh" if all(s=="fresh" for s in states) else "degraded"
                return {"database":"available","broker":{"status":status,"workers_observed":len(states),"workers_fresh":states.count("fresh")}}
            snapshot=session.execute(text("SELECT MAX(captured_at) FROM performance_account_snapshots")).scalar()
        snapshot_age=(now-snapshot).total_seconds() if snapshot else None
        return {
            "database": "available",
            "broker": {
                "status": (
                    "unavailable" if snapshot_age is None
                    else "stale"
                    if snapshot_age > 120 or snapshot_age < 0
                    else "fresh"
                ),
                "snapshot_age_seconds": snapshot_age,
                "worker_age_seconds": None,
            },
        }
    except Exception:
        # Public errors expose no connection strings or credentials.
        return {"database": "unavailable", "broker": {"status": "unavailable"}}


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    components = component_health()
    return HealthResponse(
        status="healthy" if components["broker"]["status"] == "fresh" else "degraded",
        service="super-signals-api",
        version="0.1.0",
        environment=settings.environment,
        components=components,
    )
