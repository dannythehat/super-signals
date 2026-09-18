from __future__ import annotations

import httpx
import pytest

from app import aidy_context_client as module


class _Response:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, *_args: object, **_kwargs: object) -> _Response:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_context_transport_retries_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("GET", "https://aidy.test/provider/context")
    fake = _FakeClient([httpx.ConnectTimeout("timeout", request=request), _Response(200)])
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: fake)
    monkeypatch.setattr(module.asyncio, "sleep", _no_sleep)

    client = module.AidyContextClient(base_url="https://aidy.test", bearer_token="x")
    response = await client._get_with_retry(url="https://aidy.test/provider/context")

    assert response.status_code == 200
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_context_transport_retries_read_error_and_503(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("GET", "https://aidy.test/provider/context")
    fake = _FakeClient([httpx.ReadError("read", request=request), _Response(503), _Response(200)])
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: fake)
    monkeypatch.setattr(module.asyncio, "sleep", _no_sleep)

    client = module.AidyContextClient(base_url="https://aidy.test", bearer_token="x")
    response = await client._get_with_retry(url="https://aidy.test/provider/context")

    assert response.status_code == 200
    assert fake.calls == 3


@pytest.mark.asyncio
async def test_context_transport_does_not_retry_terminal_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient([_Response(409), _Response(200)])
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **_kwargs: fake)

    client = module.AidyContextClient(base_url="https://aidy.test", bearer_token="x")
    response = await client._get_with_retry(url="https://aidy.test/provider/context")

    assert response.status_code == 409
    assert fake.calls == 1


async def _no_sleep(_seconds: float) -> None:
    return None
