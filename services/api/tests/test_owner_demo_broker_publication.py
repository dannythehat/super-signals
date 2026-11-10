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


def grouped(event="trade_entry", **changes):
    fill = {"deal_id": 301, "order_id": 401, "position_id": 401, "leg": 1, "volume": ".03",
            "price": "4047.48", "stop_loss": "4038.19", "take_profit": "4052.84",
            "reason": "broker_deal", "realised_net": "0"}
    return parse(DEAL | {
        "event": event, "opened_at": DEAL["occurred_at"], "source": "provider",
        "fills": [fill, fill | {"deal_id": 302, "leg": 2, "price": "4047.46", "take_profit": "4057.10"}],
        "realised_net": "12.04", "trade_closed": False,
    } | changes)


def test_grouped_entry_has_provider_and_actual_broker_prices_without_ticket_noise():
    key, title, body = broker_notification(grouped(), NOW, provider_name="XAUUSD JULIA")
    assert key == "owner-demo-trade-entry:700207861:440223233"
    assert title == "XAUUSD JULIA"
    assert body.splitlines() == [
        "Trade details: Buy gold at 4047.46 to 4047.48", "SL - 4038.19", "",
        "TP 1 - 4052.84", "TP 2 - 4057.10",
    ]


def test_grouped_exits_show_target_number_or_one_stop_and_total_trade_pnl():
    base = grouped().fills[0].model_dump()
    tp = base | {"leg": 2, "reason": "take_profit", "price": "4057.10"}
    _, _, text = broker_notification(grouped("trade_exit", fills=[tp]), NOW)
    assert "TP 2 HIT - 4057.10" in text and "Trade P&L so far - +$12.04" in text
    stops = [base | {"deal_id": 301+i, "leg": i+1, "reason": "stop_loss", "price": "4038.19"} for i in range(3)]
    _, _, text = broker_notification(grouped("trade_exit", fills=stops, trade_closed=True, realised_net="-83.49"), NOW)
    assert text.count("SL HIT") == 1 and "Trade P&L - -$83.49" in text


def test_tp2_protection_posts_broker_confirmed_remaining_stop():
    event = parse(DEAL | {"event": "trade_protection", "opened_at": DEAL["occurred_at"],
                          "tp2_deal_id": 777, "stops": [{"leg": 3, "stop_loss": "4076.38", "entry": "4076.38"}]})
    _, title, text = broker_notification(event, NOW, provider_name="Julia")
    assert title == "Julia"
    assert "After TP 2" in text and "TP 3 SL 4076.38" in text
