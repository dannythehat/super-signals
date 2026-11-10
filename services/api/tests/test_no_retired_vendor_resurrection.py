"""Stop retired remote-broker files or imports from returning to Super Signals.

The only production MT5 executor is the owner's Contabo bridge. This check
is intentionally independent of Render environment variables and broker secrets.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app"


def test_no_remote_vendor_modules_or_endpoints_remain() -> None:
    forbidden = [item for item in ROOT.rglob("metaapi_*.py") if item.is_file()]
    assert forbidden == [], f"Retired vendor modules reintroduced: {forbidden}"
    for source in ROOT.rglob("*.py"):
        code = source.read_text(encoding="utf-8")
        assert "agiliumtrade.ai" not in code, source
        assert "from app.metaapi_" not in code, source
        assert "import app.metaapi_" not in code, source


def test_retired_remote_gateway_cannot_trade_or_read(monkeypatch) -> None:
    import asyncio

    import pytest

    from app.broker_gateway_factory import broker_transport
    from app.retired_remote_broker_contracts import (
        MetaApiGatewayError,
        MetaApiReadGateway,
        MetaApiTradeGateway,
    )

    monkeypatch.setenv("SUPER_SIGNALS_BROKER_TRANSPORT", "metaapi")
    assert broker_transport() == "local_bridge"
    with pytest.raises(MetaApiGatewayError, match="retired_broker_api_disabled"):
        asyncio.run(MetaApiReadGateway().read_positions())
    with pytest.raises(MetaApiGatewayError, match="retired_broker_api_disabled"):
        asyncio.run(
            MetaApiTradeGateway()._trade_request(
                token="", account_id="old", region="local",
                json_body={"actionType": "ORDER_TYPE_BUY"},
            )
        )
