from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway


def test_move_to_break_even_preserves_existing_take_profit(monkeypatch) -> None:
    async def read_positions(self, *, token: str, account_id: str, region: str):
        return [
            {
                "id": "1787028586",
                "openPrice": 4393.35,
                "stopLoss": 4420.0,
                "takeProfit": 4387.0,
            }
        ]

    monkeypatch.setattr(MetaApiReadGateway, "read_positions", read_positions)
    gateway = MetaApiTradeGateway()
    request = AsyncMock(return_value={"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE"})
    monkeypatch.setattr(gateway, "_trade_request", request)

    asyncio.run(
        gateway.modify_position(
            token="x" * 30,
            account_id="account",
            region="new-york",
            position_id="1787028586",
            stop_loss=4393.35,
        )
    )

    body = request.await_args.kwargs["json_body"]
    assert body["stopLoss"] == 4393.35
    assert body["takeProfit"] == 4387.0
    assert body["stopLossUnits"] == "ABSOLUTE_PRICE"
    assert body["takeProfitUnits"] == "ABSOLUTE_PRICE"


def test_take_profit_edit_preserves_existing_stop_loss(monkeypatch) -> None:
    async def read_positions(self, *, token: str, account_id: str, region: str):
        return [
            {
                "id": "1787028595",
                "openPrice": 4393.34,
                "stopLoss": 4393.34,
                "takeProfit": 4350.0,
            }
        ]

    monkeypatch.setattr(MetaApiReadGateway, "read_positions", read_positions)
    gateway = MetaApiTradeGateway()
    request = AsyncMock(return_value={"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE"})
    monkeypatch.setattr(gateway, "_trade_request", request)

    asyncio.run(
        gateway.modify_position(
            token="x" * 30,
            account_id="account",
            region="new-york",
            position_id="1787028595",
            take_profit=4345.0,
        )
    )

    body = request.await_args.kwargs["json_body"]
    assert body["stopLoss"] == 4393.34
    assert body["takeProfit"] == 4345.0


def test_runner_without_take_profit_remains_runner(monkeypatch) -> None:
    async def read_positions(self, *, token: str, account_id: str, region: str):
        return [
            {
                "id": "runner-1",
                "openPrice": 4393.0,
                "stopLoss": 4420.0,
                "takeProfit": None,
            }
        ]

    monkeypatch.setattr(MetaApiReadGateway, "read_positions", read_positions)
    gateway = MetaApiTradeGateway()
    request = AsyncMock(return_value={"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE"})
    monkeypatch.setattr(gateway, "_trade_request", request)

    asyncio.run(
        gateway.modify_position(
            token="x" * 30,
            account_id="account",
            region="new-york",
            position_id="runner-1",
            stop_loss=4393.0,
        )
    )

    body = request.await_args.kwargs["json_body"]
    assert body["stopLoss"] == 4393.0
    assert "takeProfit" not in body
