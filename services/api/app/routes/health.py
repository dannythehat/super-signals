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
            workers = (
                session.execute(
                    text(
                        "SELECT profile, MAX(last_seen_at) AS seen FROM local_bridge_workers GROUP BY profile"
                    )
                )
                .mappings()
                .all()
            )
            local_snapshot = session.execute(
                text("""SELECT CASE WHEN COUNT(*)=COUNT(read_at) THEN MIN(read_at) END FROM (
                    SELECT w.account_id, MAX(c.completed_at) AS read_at
                    FROM local_bridge_workers w
                    LEFT JOIN local_bridge_commands c
                      ON c.profile=w.profile AND c.account_id=w.account_id
                     AND c.status='succeeded' AND c.operation='read_account_information'
                    WHERE w.profile=:profile GROUP BY w.account_id
                ) reads"""),
                {"profile": os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals")},
            ).scalar()
            snapshot = (
                local_snapshot
                if local
                else session.execute(
                    text("SELECT MAX(captured_at) FROM performance_account_snapshots")
                ).scalar()
            )
        ages = {str(w["profile"]): (now - w["seen"]).total_seconds() for w in workers if w["seen"]}
        worker_age = ages.get(profile)
        snapshot_age = (now - snapshot).total_seconds() if snapshot else None
        return {
            "database": "available",
            "broker": {
                "status": (
                    "disconnected"
                    if local and (worker_age is None or worker_age > 90)
                    else "unavailable"
                    if snapshot_age is None
                    else "stale"
                    if snapshot_age > 120 or snapshot_age < 0
                    else "fresh"
                ),
                "snapshot_age_seconds": snapshot_age,
                "worker_age_seconds": worker_age if local else None,
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
