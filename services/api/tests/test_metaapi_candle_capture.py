import asyncio

import httpx
import pytest

from app.metaapi_read_gateway import MetaApiReadGateway


class CaptureCandleGateway(MetaApiReadGateway):
    def __init__(self) -> None:
        super().__init__()
        self.captured: dict[str, object] | None = None

    async def _request(self, method: str, url: str, *, token: str) -> httpx.Response:
        self.captured = {"method": method, "url": url, "token": token}
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "XAUUSD",
                    "timeframe": "15m",
                    "time": "2026-08-14T10:00:00.000Z",
                    "brokerTime": "2026-08-14 13:00:00.000",
                    "open": 4340,
                    "high": 4350,
                    "low": 4338,
                    "close": 4348,
                    "tickVolume": 1234,
                    "spread": 20,
                    "volume": 45,
                }
            ],
        )


def test_historical_candles_use_read_only_market_data_host_and_bounded_limit() -> None:
    gateway = CaptureCandleGateway()

    rows = asyncio.run(
        gateway.read_historical_candles(
            token="secret-token",
            account_id="account-1",
            region="london",
            symbol="XAUUSD",
            timeframe="15m",
            limit=3,
        )
    )

    assert len(rows) == 1
    assert gateway.captured is not None
    assert gateway.captured["method"] == "GET"
    assert gateway.captured["token"] == "secret-token"
    assert gateway.captured["url"] == (
        "https://mt-market-data-client-api-v1.london.agiliumtrade.ai"
        "/users/current/accounts/account-1/historical-market-data/"
        "symbols/XAUUSD/timeframes/15m/candles?limit=3"
    )


@pytest.mark.parametrize("timeframe", ["tick", "M5", "", "7m"])
def test_historical_candles_reject_unknown_timeframe(timeframe: str) -> None:
    gateway = CaptureCandleGateway()
    with pytest.raises(ValueError, match="timeframe"):
        asyncio.run(
            gateway.read_historical_candles(
                token="secret-token",
                account_id="account-1",
                region="london",
                symbol="XAUUSD",
                timeframe=timeframe,
            )
        )


@pytest.mark.parametrize("limit", [0, 1001])
def test_historical_candles_reject_unbounded_limit(limit: int) -> None:
    gateway = CaptureCandleGateway()
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(
            gateway.read_historical_candles(
                token="secret-token",
                account_id="account-1",
                region="london",
                symbol="XAUUSD",
                timeframe="1m",
                limit=limit,
            )
        )


def test_read_gateway_still_exposes_only_read_methods() -> None:
    public_names = {name for name in dir(MetaApiReadGateway) if not name.startswith("_")}
    assert "read_historical_candles" in public_names
    assert all(
        name == "resolve_account_region" or name.startswith("read_")
        for name in public_names
    )
