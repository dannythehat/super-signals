"""Contract tests for the universal Owner MT5 onboarding/reconnect flow."""

from __future__ import annotations

import asyncio

import pytest

# Import the connection router before the production app is built, matching runtime.
from app.routes import member_connection_v2 as _member_connection_v2  # noqa: F401
from app.main import app as production_app
from app.metaapi_gateway import MetaApiGatewayError
from app.routes.member_connection_v2 import (
    OnboardConnectionV2Request,
    _known_broker_name,
)


class _KnownServerResponse:
    content = b"known-servers"

    def __init__(self, payload: dict[str, list[str]]) -> None:
        self._payload = payload

    def json(self) -> dict[str, list[str]]:
        return self._payload


class _KnownServerGateway:
    def __init__(self, payload: dict[str, list[str]]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str, list[tuple[str, str]] | None]] = []

    async def _request(self, method: str, path: str, *, token: str, params=None, **_kwargs):
        assert token == "metaapi-token"
        self.calls.append((method, path, params))
        return _KnownServerResponse(self.payload)


class _Service:
    def __init__(self, payload: dict[str, list[str]]) -> None:
        self._gateway = _KnownServerGateway(payload)


def test_connection_v2_routes_are_registered_in_production_api() -> None:
    route_methods = {
        (route.path, method)
        for route in production_app.routes
        for method in (getattr(route, "methods", None) or set())
    }

    assert ("/admin/accounts/members/onboard-live-v2", "POST") in route_methods
    assert ("/admin/accounts/members/{user_id}/connection-v2", "POST") in route_methods
    assert ("/admin/accounts/members/{user_id}/connection-v2", "GET") in route_methods


def test_owner_onboarding_password_is_secret_in_model_output() -> None:
    payload = OnboardConnectionV2Request(
        email="member@example.com",
        display_name="Member",
        mt5_login="12345678",
        mt5_password="never-log-this-password",
        mt5_server="VantageMarkets-Live 10",
        complimentary_access=True,
    )

    assert "never-log-this-password" not in repr(payload)
    assert "never-log-this-password" not in str(payload)


def test_exact_metaapi_broker_is_resolved_from_exact_server() -> None:
    service = _Service(
        {
            "Vantage Markets International Ltd": ["VantageMarkets-Live 7"],
            "Vantage Markets (Pty) Ltd": ["VantageMarkets-Live 10"],
        }
    )

    broker = asyncio.run(
        _known_broker_name(service, "metaapi-token", "VantageMarkets-Live 10")  # type: ignore[arg-type]
    )

    assert broker == "Vantage Markets (Pty) Ltd"
    assert service._gateway.calls == [
        (
            "GET",
            "/known-mt-servers/5/search",
            [("query", "VantageMarkets-Live 10")],
        )
    ]


def test_unknown_server_fails_before_creating_a_terminal() -> None:
    service = _Service({"Vantage Markets (Pty) Ltd": ["VantageMarkets-Live 10"]})

    with pytest.raises(MetaApiGatewayError) as exc_info:
        asyncio.run(
            _known_broker_name(service, "metaapi-token", "Wrong-Live-Server")  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "metaapi_server_not_known"
