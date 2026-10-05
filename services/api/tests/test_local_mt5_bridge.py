from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.broker_gateway_factory import build_broker_gateways
from app.local_bridge_gateways import (
    LocalBridgeMarginGateway,
    LocalBridgeReadGateway,
    LocalBridgeTradeGateway,
)


class _Queue:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
async def test_market_order_reuses_canonical_contract_and_idempotency() -> None:
    queue = _Queue(
        {
            "orderId": "701",
            "positionId": "801",
            "numericCode": 10009,
            "stringCode": "TRADE_RETCODE_DONE",
        }
    )
    read = LocalBridgeReadGateway(queue)  # type: ignore[arg-type]
    gateway = LocalBridgeTradeGateway(queue, read_gateway=read)  # type: ignore[arg-type]

    result = await gateway.place_market_order(
        token="ignored-local-reference",
        account_id="account-ref",
        region="local",
        side="BUY",
        symbol="XAUUSD",
        volume=0.03,
        stop_loss=2600.0,
        take_profit=2650.0,
        client_id="abc_def_123",
    )

    assert result.order_id == "701"
    assert result.position_id == "801"
    assert queue.calls == [
        {
            "account_id": "account-ref",
            "operation": "trade",
            "payload": {
                "actionType": "ORDER_TYPE_BUY",
                "symbol": "XAUUSD",
                "volume": 0.03,
                "stopLoss": 2600.0,
                "stopLossUnits": "ABSOLUTE_PRICE",
                "takeProfit": 2650.0,
                "takeProfitUnits": "ABSOLUTE_PRICE",
                "clientId": "abc_def_123",
            },
            "idempotency_key": "mt5-order:account-ref:abc_def_123",
        }
    ]


@pytest.mark.asyncio
async def test_read_gateway_preserves_broker_payload() -> None:
    expected = [{"id": "801", "clientId": "abc_def_123"}]
    queue = _Queue(expected)
    gateway = LocalBridgeReadGateway(queue)  # type: ignore[arg-type]

    result = await gateway.read_positions(
        token="ignored",
        account_id="account-ref",
        region="local",
    )

    assert result == expected
    assert queue.calls[0]["operation"] == "read_positions"


@pytest.mark.asyncio
async def test_read_gateway_maps_local_demo_balance_to_canonical_paper_ledger() -> None:
    queue = _Queue(
        {
            "currency": "USD",
            "balance": 10020.0,
            "credit": 0.0,
            "equity": 10025.0,
            "margin": 10.0,
            "freeMargin": 10015.0,
            "marginLevel": 100250.0,
            "leverage": 500,
            "tradeAllowed": True,
        }
    )
    gateway = LocalBridgeReadGateway(
        queue,  # type: ignore[arg-type]
        raw_balance_baseline=Decimal("10000"),
        canonical_balance_baseline=Decimal("2428.78"),
    )

    result = await gateway.read_account_information(
        token="ignored",
        account_id="account-ref",
        region="local",
    )

    assert result["balance"] == pytest.approx(2448.78)
    assert result["equity"] == pytest.approx(2453.78)
    assert result["freeMargin"] == pytest.approx(2443.78)
    assert result["marginLevel"] == pytest.approx(24537.8)
    assert result["tradeAllowed"] is True


def test_read_gateway_rejects_partial_balance_baseline_configuration() -> None:
    with pytest.raises(ValueError, match="local_bridge_balance_baselines_incomplete"):
        LocalBridgeReadGateway(
            _Queue({}),  # type: ignore[arg-type]
            raw_balance_baseline=Decimal("10000"),
        )


def test_factory_selects_local_transport_only_when_explicit(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_BROKER_TRANSPORT", "local_bridge")
    monkeypatch.setenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals")
    gateways = build_broker_gateways(object())  # type: ignore[arg-type]

    assert isinstance(gateways.read, LocalBridgeReadGateway)
    assert isinstance(gateways.trade, LocalBridgeTradeGateway)
    assert isinstance(gateways.margin, LocalBridgeMarginGateway)


def test_factory_applies_configured_local_bridge_balance_baselines(monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_BROKER_TRANSPORT", "local_bridge")
    monkeypatch.setenv("SUPER_SIGNALS_LOCAL_BRIDGE_RAW_BALANCE_BASELINE", "10000")
    monkeypatch.setenv("SUPER_SIGNALS_LOCAL_BRIDGE_CANONICAL_BALANCE_BASELINE", "2428.78")

    gateways = build_broker_gateways(object())  # type: ignore[arg-type]

    assert isinstance(gateways.read, LocalBridgeReadGateway)
    assert gateways.read._account_value_offset == Decimal("-7571.22")  # noqa: SLF001
