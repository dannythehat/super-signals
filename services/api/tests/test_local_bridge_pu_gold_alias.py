"""PU Prime gold alias is stable with the existing Windows worker."""
from __future__ import annotations

import pytest

from app.local_bridge_gateways import (
    LocalBridgeMarginGateway, LocalBridgeReadGateway,
    LocalBridgeTradeGateway, _bridge_compatible_symbol,
)


class StubQueue:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, *, account_id, operation, payload=None, idempotency_key=None):
        self.calls.append((account_id, operation, payload, idempotency_key))
        if operation == "trade":
            return {"orderId": "111", "positionId": "222", "numericCode": 10009}
        if operation == "calculate_margin":
            return {"margin": 10.0}
        return {"symbol": "XAUUSD.s"}


def test_source_symbol_unmodified_and_pu_alias_mapped():
    assert _bridge_compatible_symbol("XAUUSD") == "XAUUSD"
    assert _bridge_compatible_symbol("XAUUSD.s") == "XAUUSD"
    assert _bridge_compatible_symbol("EURUSD") == "EURUSD"


@pytest.mark.asyncio
async def test_pu_specification_maps_suffix_to_worker_alias():
    queue = StubQueue()
    gw = LocalBridgeReadGateway(queue)
    result = await gw.read_symbol_specification(
        token="", account_id="pu-prime-live-36394935", region="local",
        symbol="XAUUSD.s",
    )
    assert result["symbol"] == "XAUUSD.s"
    assert queue.calls[0][2] == {"symbol": "XAUUSD"}


@pytest.mark.asyncio
async def test_pu_order_preserves_client_id_and_exact_volume():
    queue = StubQueue()
    read = LocalBridgeReadGateway(queue)
    gw = LocalBridgeTradeGateway(queue, read_gateway=read)
    result = await gw.place_market_order(
        token="", account_id="pu-prime-live-36394935", region="local",
        side="BUY", symbol="XAUUSD.s", volume=0.01, stop_loss=3990.0,
        take_profit=4010.0, client_id="SS_PU_12345",
    )
    assert result.position_id == "222"
    account, operation, payload, key = queue.calls[0]
    assert operation == "trade"
    assert payload["symbol"] == "XAUUSD"
    assert payload["volume"] == 0.01
    assert payload["stopLoss"] == 3990.0
    assert payload["clientId"] == "SS_PU_12345"
    assert key == "mt5-order:pu-prime-live-36394935:SS_PU_12345"


@pytest.mark.asyncio
async def test_pu_margin_uses_worker_alias():
    queue = StubQueue()
    gw = LocalBridgeMarginGateway(queue)
    await gw.calculate_margin(
        token="", account_id="pu-prime-live-36394935", region="local",
        symbol="XAUUSD.s", side="SELL", volume=0.01, open_price=4000,
    )
    assert queue.calls[0][2]["symbol"] == "XAUUSD"
