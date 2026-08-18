from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.owner_manual_close as manual_close
from app.metaapi_gateway import MetaApiGatewayError
from app.owner_manual_close import OwnerManualCloseService
from app.routes.manual_reconciliation_day36 import ManualActionResponse
from app.routes.owner_manual_close import _owner


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"ciphertext"
        return "token"


class _ReadGateway:
    def __init__(self, broker_ids: list[str]) -> None:
        self.broker_ids = broker_ids

    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        assert token == "token"
        assert account_id == "account"
        return "new-york"

    async def read_positions(self, *, token: str, account_id: str, region: str):
        return [{"id": value} for value in self.broker_ids]

    async def read_deals_by_position(self, *, token: str, account_id: str, region: str, position_id: str):
        return [{"entryType": "DEAL_ENTRY_OUT", "price": "4392.5", "time": "2026-08-18T12:00:00Z"}]


class _TradeGateway:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.closed: list[str] = []
        self.failing = failing or set()

    async def close_position(self, *, token: str, account_id: str, region: str, position_id: str) -> None:
        if position_id in self.failing:
            raise MetaApiGatewayError("metaapi_temporarily_unavailable", retryable=True)
        self.closed.append(position_id)


def _service(monkeypatch, *, broker_ids: list[str], failing: set[str] | None = None):
    trade = _TradeGateway(failing)
    service = OwnerManualCloseService(
        session_factory=SimpleNamespace(),
        cipher=_Cipher(),
        read_gateway=_ReadGateway(broker_ids),
        trade_gateway=trade,
    )
    monkeypatch.setattr(
        service,
        "_account",
        lambda user_id: manual_close._Account(
            account_id="account",
            account_environment="demo",
            token_ciphertext=b"ciphertext",
        ),
    )
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service,
        "_record_close",
        lambda **kwargs: recorded.append((str(kwargs["position"].id), kwargs["scope"])),
    )
    return service, trade, recorded


@pytest.mark.asyncio
async def test_close_trade_addresses_each_exact_broker_position_without_symbol_close(monkeypatch) -> None:
    user_id = uuid4()
    signal_id = uuid4()
    first = manual_close._Position(id=uuid4(), signal_id=signal_id, broker_position_id="broker-1")
    second = manual_close._Position(id=uuid4(), signal_id=signal_id, broker_position_id="broker-2")
    service, trade, recorded = _service(monkeypatch, broker_ids=["broker-1", "broker-2"])
    monkeypatch.setattr(service, "_positions", lambda **kwargs: (first, second))

    result = await service.close_trade(user_id, signal_id)

    assert trade.closed == ["broker-1", "broker-2"]
    assert result.closed_count == 2
    assert result.failed_count == 0
    assert result.closed_position_ids == (first.id, second.id)
    assert recorded == [(str(first.id), "trade"), (str(second.id), "trade")]


@pytest.mark.asyncio
async def test_close_position_does_not_retry_a_position_already_absent_at_broker(monkeypatch) -> None:
    user_id = uuid4()
    position = manual_close._Position(id=uuid4(), signal_id=uuid4(), broker_position_id="already-gone")
    service, trade, recorded = _service(monkeypatch, broker_ids=[])
    monkeypatch.setattr(service, "_positions", lambda **kwargs: (position,))

    result = await service.close_position(user_id, position.id)

    assert trade.closed == []
    assert recorded == []
    assert result.closed_count == 0
    assert result.already_closed_count == 1


@pytest.mark.asyncio
async def test_whole_trade_keeps_successful_closes_when_one_broker_close_fails(monkeypatch) -> None:
    user_id = uuid4()
    signal_id = uuid4()
    first = manual_close._Position(id=uuid4(), signal_id=signal_id, broker_position_id="broker-1")
    second = manual_close._Position(id=uuid4(), signal_id=signal_id, broker_position_id="broker-2")
    service, trade, recorded = _service(
        monkeypatch,
        broker_ids=["broker-1", "broker-2"],
        failing={"broker-2"},
    )
    monkeypatch.setattr(service, "_positions", lambda **kwargs: (first, second))

    result = await service.close_trade(user_id, signal_id)

    assert trade.closed == ["broker-1"]
    assert result.closed_count == 1
    assert result.failed_count == 1
    assert result.failed_position_ids == (second.id,)
    assert recorded == [(str(first.id), "trade")]


def test_owner_route_is_server_enforced() -> None:
    _owner({"role": "owner"})
    with pytest.raises(HTTPException) as caught:
        _owner({"role": "trading_admin"})
    assert caught.value.status_code == 403


def test_manual_activity_accepts_bigint_audit_ids() -> None:
    item = ManualActionResponse(
        audit_id=34065,
        position_id=uuid4(),
        action_type="position_closed",
        label="Manual action outside the app",
        detail="Trade · Open → Closed",
        old_value="open",
        new_value="closed",
        trade_reference="SS-TEST",
        occurred_at=datetime.now(UTC),
    )
    assert item.audit_id == 34065
