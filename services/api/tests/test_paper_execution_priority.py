from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_execution_day26 import Day26ExecutionError
from app.paper_execution_priority import PaperExecutionPriorityService
from app import paper_resilient_read_gateway as resilient
from app.paper_resilient_read_gateway import PaperResilientMetaApiReadGateway


def _service(max_age: float = 90.0) -> PaperExecutionPriorityService:
    service = object.__new__(PaperExecutionPriorityService)
    service._paper_max_signal_age_seconds = max_age
    return service


def test_market_layer_uses_current_market_even_after_quote_moves() -> None:
    entries = (
        SimpleNamespace(order_type="market", price=Decimal("4398")),
        SimpleNamespace(order_type="buy_limit", price=Decimal("4397")),
        SimpleNamespace(order_type="buy_limit", price=Decimal("4396")),
    )
    _service()._validate_entry_timing(entries, "BUY", Decimal("4401"))


def test_literal_pending_order_still_obeys_broker_side_semantics() -> None:
    entries = (
        SimpleNamespace(order_type="market", price=Decimal("4398")),
        SimpleNamespace(order_type="buy_limit", price=Decimal("4397")),
    )
    with pytest.raises(Day26ExecutionError, match="pending_entry_no_longer_valid"):
        _service()._validate_entry_timing(entries, "BUY", Decimal("4396.5"))


def test_old_backlog_signal_is_still_rejected_by_time() -> None:
    signal = SimpleNamespace(source_posted_at=datetime.now(UTC) - timedelta(seconds=91))
    with pytest.raises(Day26ExecutionError, match="paper_signal_stale_by_time"):
        _service(max_age=90)._assert_signal_recent(signal)


def test_fresh_signal_survives_time_safety_net() -> None:
    signal = SimpleNamespace(source_posted_at=datetime.now(UTC) - timedelta(seconds=20))
    _service(max_age=90)._assert_signal_recent(signal)


@pytest.mark.asyncio
async def test_demo_read_gateway_retries_retryable_gets(monkeypatch) -> None:
    calls = 0

    async def fake_request(self, method, url, *, token):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise MetaApiGatewayError("metaapi_temporarily_unavailable", retryable=True)
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(MetaApiReadGateway, "_request", fake_request)
    gateway = PaperResilientMetaApiReadGateway(
        attempts=3,
        retry_delay_seconds=0,
    )
    response = await gateway._request("GET", "https://example.invalid", token="x")
    assert response.status_code == 200
    assert calls == 3


@pytest.mark.asyncio
async def test_demo_read_gateway_does_not_retry_nonretryable_errors(monkeypatch) -> None:
    calls = 0

    async def fake_request(self, method, url, *, token):
        nonlocal calls
        calls += 1
        raise MetaApiGatewayError("metaapi_token_invalid", retryable=False)

    monkeypatch.setattr(MetaApiReadGateway, "_request", fake_request)
    gateway = PaperResilientMetaApiReadGateway(
        attempts=3,
        retry_delay_seconds=0,
    )
    with pytest.raises(MetaApiGatewayError, match="metaapi_token_invalid"):
        await gateway._request("GET", "https://example.invalid", token="x")
    assert calls == 1


@pytest.mark.asyncio
async def test_account_information_timeouts_request_one_terminal_self_heal(monkeypatch) -> None:
    calls = 0
    redeploys: list[tuple[str, str]] = []

    async def always_timeout(self, method, url, *, token):
        nonlocal calls
        calls += 1
        raise MetaApiGatewayError("metaapi_timeout", retryable=True)

    async def fake_redeploy(self, *, token, account_id):
        redeploys.append((token, account_id))

    monkeypatch.setattr(MetaApiReadGateway, "_request", always_timeout)
    monkeypatch.setattr(PaperResilientMetaApiReadGateway, "_redeploy_stalled_terminal", fake_redeploy)
    gateway = PaperResilientMetaApiReadGateway(attempts=3, retry_delay_seconds=0)

    with pytest.raises(MetaApiGatewayError, match="metaapi_timeout"):
        await gateway._request(
            "GET",
            "https://mt-client-api-v1.london.agiliumtrade.ai/users/current/accounts/account-123/account-information",
            token="test-token",
        )

    assert calls == 3
    assert redeploys == [("test-token", "account-123")]


@pytest.mark.asyncio
async def test_other_terminal_read_timeouts_never_request_redeploy(monkeypatch) -> None:
    calls = 0
    redeploys: list[tuple[str, str]] = []

    async def always_timeout(self, method, url, *, token):
        nonlocal calls
        calls += 1
        raise MetaApiGatewayError("metaapi_timeout", retryable=True)

    async def fake_redeploy(self, *, token, account_id):
        redeploys.append((token, account_id))

    monkeypatch.setattr(MetaApiReadGateway, "_request", always_timeout)
    monkeypatch.setattr(PaperResilientMetaApiReadGateway, "_redeploy_stalled_terminal", fake_redeploy)
    gateway = PaperResilientMetaApiReadGateway(attempts=3, retry_delay_seconds=0)

    with pytest.raises(MetaApiGatewayError, match="metaapi_timeout"):
        await gateway._request(
            "GET",
            "https://mt-client-api-v1.london.agiliumtrade.ai/users/current/accounts/account-123/positions",
            token="test-token",
        )

    assert calls == 3
    assert redeploys == []


@pytest.mark.asyncio
async def test_terminal_self_heal_has_process_cooldown(monkeypatch) -> None:
    resilient._LAST_TERMINAL_REDEPLOY_ATTEMPT.clear()
    posts: list[tuple[str, dict[str, str]]] = []

    class _Response:
        status_code = 202

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, headers):
            posts.append((url, headers))
            return _Response()

    monkeypatch.setattr(resilient.httpx, "AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setattr(resilient.time, "monotonic", lambda: 1000.0)
    gateway = PaperResilientMetaApiReadGateway(attempts=3, retry_delay_seconds=0)

    await gateway._redeploy_stalled_terminal(token="secret-test-token", account_id="account-123")
    await gateway._redeploy_stalled_terminal(token="secret-test-token", account_id="account-123")

    assert len(posts) == 1
    assert posts[0][0].endswith("/users/current/accounts/account-123/redeploy")
    assert posts[0][1]["auth-token"] == "secret-test-token"
