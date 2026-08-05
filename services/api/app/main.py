"""FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routes.health import router as health_router


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="Super Signals API",
        version="0.1.0",
        description="Private signal-processing and trading infrastructure.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET"],
        allow_headers=["Accept", "Content-Type"],
    )
    application.include_router(health_router)

    @application.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"name": "Super Signals API", "docs": "/docs"}

    return application


app = create_app()
