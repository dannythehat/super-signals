import os

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_endpoint_returns_service_state(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.routes.health.component_health",
        lambda: {"database": "available", "broker": {"status": "fresh"}},
    )
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "service": "super-signals-api",
        "version": "0.1.0",
        "environment": os.getenv("APP_ENV", "development"),
        "components": {"database": "available", "broker": {"status": "fresh"}},
    }


def test_root_endpoint_is_safe_and_minimal() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"name": "Super Signals API", "docs": "/docs"}
