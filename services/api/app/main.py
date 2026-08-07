"""FastAPI application entry point."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routes.access import router as access_router
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.telegram_accounts import router as telegram_accounts_router
from app.routes.telegram_sources import router as telegram_sources_router


def _mount_web_application(application: FastAPI) -> None:
    web_dist_value = os.getenv("SUPER_SIGNALS_WEB_DIST")
    if not web_dist_value:

        @application.get("/", include_in_schema=False)
        def root() -> dict[str, str]:
            return {"name": "Super Signals API", "docs": "/docs"}

        return

    web_dist = Path(web_dist_value)
    if not web_dist.is_dir():
        raise RuntimeError(f"SUPER_SIGNALS_WEB_DIST does not exist: {web_dist}")
    application.mount("/", StaticFiles(directory=web_dist, html=True), name="web")


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="Super Signals API",
        version="0.6.0",
        description="Private signal-processing and trading infrastructure.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID"],
    )
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(access_router)
    application.include_router(telegram_accounts_router)
    application.include_router(telegram_sources_router)
    _mount_web_application(application)
    return application


app = create_app()
