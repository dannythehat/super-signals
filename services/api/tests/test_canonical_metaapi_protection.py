from __future__ import annotations

import pytest

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_trade_gateway import MetaApiTradeGateway


@pytest.mark.asyncio
async def test_sl_only_modify_preserves_broker_take_profit(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = MetaApiTradeGateway()
    sent: list[dict[str, object]] = []

    async def read_positions(**_kwargs):
        return [{"id": "P1", "stopLoss": 4300.0, "takeProfit": 4400.0}]

    async def trade_request(**kwargs):
        sent.append(dict(kwargs["json_body"]))
        return {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE"}

    monkeypatch.setattr(gateway._read, "read_positions", read_positions)
    monkeypatch.setattr(gateway, "_trade_request", trade_request)

    await gateway.modify_position(
        token="fixture",
        account_id="account",
        region="london",
        position_id="P1",
        stop_loss=4350.0,
    )

    assert sent == [
        {
            "actionType": "POSITION_MODIFY",
            "positionId": "P1",
            "stopLoss": 4350.0,
            "stopLossUnits": "ABSOLUTE_PRICE",
            "takeProfit": 4400.0,
            "takeProfitUnits": "ABSOLUTE_PRICE",
        }
    ]


@pytest.mark.asyncio
async def test_tp_only_modify_preserves_broker_stop_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = MetaApiTradeGateway()
    sent: list[dict[str, object]] = []

    async def read_positions(**_kwargs):
        return [{"id": "P1", "stopLoss": 4300.0, "takeProfit": 4400.0}]

    async def trade_request(**kwargs):
        sent.append(dict(kwargs["json_body"]))
        return {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE"}

    monkeypatch.setattr(gateway._read, "read_positions", read_positions)
    monkeypatch.setattr(gateway, "_trade_request", trade_request)

    await gateway.modify_position(
        token="fixture",
        account_id="account",
        region="london",
        position_id="P1",
        take_profit=4450.0,
    )

    assert sent == [
        {
            "actionType": "POSITION_MODIFY",
            "positionId": "P1",
            "stopLoss": 4300.0,
            "stopLossUnits": "ABSOLUTE_PRICE",
            "takeProfit": 4450.0,
            "takeProfitUnits": "ABSOLUTE_PRICE",
        }
    ]


@pytest.mark.asyncio
async def test_sl_only_modify_allows_true_runner_without_take_profit(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = MetaApiTradeGateway()
    sent: list[dict[str, object]] = []

    async def read_positions(**_kwargs):
        return [{"id": "P1", "stopLoss": 4300.0, "takeProfit": None}]

    async def trade_request(**kwargs):
        sent.append(dict(kwargs["json_body"]))
        return {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE"}

    monkeypatch.setattr(gateway._read, "read_positions", read_positions)
    monkeypatch.setattr(gateway, "_trade_request", trade_request)

    await gateway.modify_position(
        token="fixture",
        account_id="account",
        region="london",
        position_id="P1",
        stop_loss=4350.0,
    )

    assert "takeProfit" not in sent[0]
    assert sent[0]["stopLoss"] == 4350.0


@pytest.mark.asyncio
async def test_modify_fails_closed_when_broker_position_cannot_be_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = MetaApiTradeGateway()

    async def read_positions(**_kwargs):
        return []

    monkeypatch.setattr(gateway._read, "read_positions", read_positions)

    with pytest.raises(MetaApiGatewayError) as exc_info:
        await gateway.modify_position(
            token="fixture",
            account_id="account",
            region="london",
            position_id="P1",
            stop_loss=4350.0,
        )

    assert exc_info.value.code == "broker_position_mapping_missing"
