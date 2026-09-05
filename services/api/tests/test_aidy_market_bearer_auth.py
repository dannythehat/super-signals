from __future__ import annotations

from app.aidy_market_client import AidyMarketClient


def test_aidy_market_client_sends_bearer_token() -> None:
    client = AidyMarketClient(base_url="https://aidy.example", bearer_token="secret-token")
    headers = client._headers()
    assert headers["Authorization"] == "Bearer secret-token"
    assert "X-AIDY-Signature" not in headers
    assert "X-AIDY-Timestamp" not in headers


def test_aidy_market_client_environment_requires_token(monkeypatch) -> None:
    monkeypatch.setenv("AIDY_PROVIDER_MARKET_URL", "https://aidy.example")
    monkeypatch.delenv("AIDY_PROVIDER_MARKET_TOKEN", raising=False)
    assert AidyMarketClient.from_environment() is None


def test_aidy_market_client_environment_reads_shared_token(monkeypatch) -> None:
    monkeypatch.setenv("AIDY_PROVIDER_MARKET_URL", "https://aidy.example")
    monkeypatch.setenv("AIDY_PROVIDER_MARKET_TOKEN", "shared-token")
    client = AidyMarketClient.from_environment()
    assert client is not None
    assert client._headers()["Authorization"] == "Bearer shared-token"
