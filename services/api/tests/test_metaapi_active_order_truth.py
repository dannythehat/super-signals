from __future__ import annotations

import pytest

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway


class _Gateway(MetaApiReadGateway):
    def __init__(self, payload: object) -> None:
        super().__init__()
        self.payload = payload

    async def _read_terminal_json(self, *, token: str, region: str, path: str) -> object:
        _ = token, region, path
        return self.payload


@pytest.mark.asyncio
async def test_read_orders_returns_only_live_pending_entry_orders() -> None:
    gateway = _Gateway(
        [
            {
                "id": "live-limit",
                "type": "ORDER_TYPE_BUY_LIMIT",
                "state": "ORDER_STATE_PLACED",
                "volume": 0.12,
                "currentVolume": 0.12,
            },
            {
                "id": "live-stop",
                "type": "ORDER_TYPE_SELL_STOP",
                "state": "ORDER_STATE_PARTIAL",
                "volume": 0.08,
                "currentVolume": 0.03,
            },
            {
                "id": "filled-old-order",
                "type": "ORDER_TYPE_BUY_LIMIT",
                "state": "ORDER_STATE_FILLED",
                "volume": 0.10,
                "currentVolume": 0,
            },
            {
                "id": "closed-market-order",
                "type": "ORDER_TYPE_BUY",
                "state": "ORDER_STATE_FILLED",
                "volume": 0.10,
                "currentVolume": 0,
            },
            {
                "id": "zero-remaining",
                "type": "ORDER_TYPE_SELL_LIMIT",
                "state": "ORDER_STATE_PLACED",
                "volume": 0.10,
                "currentVolume": 0,
            },
        ]
    )

    orders = await gateway.read_orders(token="token", account_id="account", region="london")

    assert [item["id"] for item in orders] == ["live-limit", "live-stop"]


@pytest.mark.asyncio
async def test_read_orders_does_not_turn_terminal_order_objects_into_pending_trades() -> None:
    gateway = _Gateway(
        [
            {
                "id": "old-1",
                "type": "ORDER_TYPE_BUY_LIMIT",
                "state": "ORDER_STATE_CANCELED",
                "volume": 0.12,
                "currentVolume": 0.12,
            },
            {
                "id": "old-2",
                "type": "ORDER_TYPE_SELL_STOP",
                "state": "ORDER_STATE_REJECTED",
                "volume": 0.12,
                "currentVolume": 0.12,
            },
            {
                "id": "old-3",
                "type": "ORDER_TYPE_SELL_LIMIT",
                "state": "ORDER_STATE_EXPIRED",
                "volume": 0.12,
                "currentVolume": 0.12,
            },
        ]
    )

    assert await gateway.read_orders(token="token", account_id="account", region="london") == []


@pytest.mark.asyncio
async def test_matching_pending_order_with_missing_broker_state_is_unknown_not_pending() -> None:
    gateway = _Gateway(
        [
            {
                "id": "ambiguous",
                "type": "ORDER_TYPE_BUY_LIMIT",
                "volume": 0.12,
                "currentVolume": 0.12,
            }
        ]
    )

    with pytest.raises(MetaApiGatewayError, match="metaapi_invalid_response"):
        await gateway.read_orders(token="token", account_id="account", region="london")
