"""FastAPI application entry point."""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import get_session_factory
from app.routes.access import router as access_router
from app.routes.admin_accounts import router as admin_accounts_router
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.telegram_accounts import router as telegram_accounts_router
from app.routes.telegram_messages import router as telegram_messages_router
from app.routes.telegram_reliability import (
    provide_day14_telegram_source_service,
    router as telegram_reliability_router,
)
from app.routes.telegram_sources import (
    provide_telegram_source_service,
    router as telegram_sources_router,
)
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import TelegramListenerManager
from app.telegram_listener_day14 import build_day14_listener_manager


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    listener: TelegramListenerManager | None = None
    if (
        settings.telegram_listener_enabled
        and settings.telegram_api_id is not None
        and settings.telegram_api_hash is not None
    ):
        listener = build_day14_listener_manager(
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            cipher=TelegramSessionCipher(settings.telegram_session_keys),
            session_factory=get_session_factory(),
            refresh_seconds=settings.telegram_listener_refresh_seconds,
        )
        application.state.telegram_listener = listener
        await listener.start()

    try:
        yield
    finally:
        if listener is not None:
            await listener.stop()


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
        version="0.9.0",
        description="Private signal-processing and trading infrastructure.",
        lifespan=_lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID"],
    )
    # Day 14 keeps the existing source-management HTTP contract but swaps in the
    # reliability-aware service implementation underneath it.
    application.dependency_overrides[provide_telegram_source_service] = (
        provide_day14_telegram_source_service
    )
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(access_router)
    application.include_router(admin_accounts_router)
    application.include_router(telegram_accounts_router)
    application.include_router(telegram_sources_router)
    application.include_router(telegram_reliability_router)
    application.include_router(telegram_messages_router)
    _mount_web_application(application)
    return application


app = create_app()
