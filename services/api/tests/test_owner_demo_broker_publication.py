from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from app.owner_demo_broker_publication import BrokerNotice, broker_notification

NOW = datetime(2026, 11, 10, 12, 41, 5, tzinfo=UTC)
DEAL = {
    "master_login": 700207861, "server": "PUPrime-Demo",
    "position_id": 440223233, "order_id": 440223233, "deal_id": 316261420,
    "symbol": "XAUUSD.s", "side": "BUY", "event": "entry", "source": "manual",
    "reason": "broker_deal", "price": "4076.38", "volume": ".01",
    "occurred_at": NOW - timedelta(seconds=1), "realised_net": Decimal("0"),
    "stop_loss": "4067", "take_profit": "4085",
}


def parse(data):
    return TypeAdapter(BrokerNotice).validate_python(data)


def test_manual_and_provider_entries_use_broker_deal_keys_and_actual_fill_values():
    for source in ("manual", "provider"):
        key, title, body = broker_notification(parse(DEAL | {"source": source}), NOW)
        assert key == "owner-demo-deal:700207861:316261420"
        assert title.endswith("MASTER ENTRY")
        assert "BUY XAUUSD.s" in body
        assert "0.01 lots at 4076.38" in body
        assert "14:41:04 Sofia" in body
        assert f"Source: {source.title()}" in body


def test_exit_and_protection_updates_have_independent_receipt_keys():
    key, title, body = broker_notification(parse(DEAL | {
        "event": "exit", "deal_id": 316261421, "reason": "take_profit", "realised_net": "8.62",
    }), NOW)
    assert key.endswith(":316261421")
    assert title.endswith("MASTER EXIT")
    assert "take profit" in body and "+8.62" in body
    key, title, body = broker_notification(parse(DEAL | {
        "event": "protection_update", "observed_at_ms": 1794314464000, "stop_loss": "4076.38",
    }), NOW)
    assert key.endswith(":440223233:1794314464000")
    assert title.endswith("MASTER PROTECTION UPDATE")
    assert "SL 4076.38" in body


def test_rejects_wrong_master_missing_fill_and_stale_event():
    for changes in ({"master_login": 25911913}, {"deal_id": 0}, {"price": "nan"}, {"volume": "0"}):
        with pytest.raises(ValidationError):
            parse(DEAL | changes)
    with pytest.raises(HTTPException):
        broker_notification(parse(DEAL), NOW + timedelta(days=2))
