"""Publish forward broker-confirmed owner master events through the existing outbox."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import get_session_factory

PositivePrice = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
OptionalPrice = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class BrokerEventBase(BaseModel):
    master_login: Literal[700207861]
    server: Literal["PUPrime-Demo"]
    position_id: int = Field(gt=0)
    symbol: str = Field(pattern=r"^[A-Za-z0-9._#-]{1,32}$")
    side: Literal["BUY", "SELL"]
    occurred_at: datetime


class BrokerDealNotice(BrokerEventBase):
    event: Literal["entry", "exit"]
    order_id: int = Field(gt=0)
    deal_id: int = Field(gt=0)
    volume: PositivePrice
    price: PositivePrice
    source: Literal["provider", "manual", "master"]
    provider_signal_prefix: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}$")
    reason: Literal["stop_loss", "take_profit", "broker_deal"]
    stop_loss: OptionalPrice | None = None
    take_profit: OptionalPrice | None = None
    realised_net: Decimal = Field(allow_inf_nan=False)


class BrokerTradeFill(BaseModel):
    deal_id: int = Field(gt=0)
    order_id: int = Field(gt=0)
    position_id: int = Field(gt=0)
    leg: int = Field(ge=1, le=5)
    volume: PositivePrice
    price: PositivePrice
    stop_loss: OptionalPrice | None = None
    take_profit: OptionalPrice | None = None
    reason: Literal["stop_loss", "take_profit", "broker_deal"]
    realised_net: Decimal = Field(allow_inf_nan=False)


class BrokerTradeNotice(BrokerEventBase):
    event: Literal["trade_entry", "trade_exit"]
    opened_at: datetime
    source: Literal["provider", "manual", "master"]
    provider_signal_prefix: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}$")
    fills: list[BrokerTradeFill] = Field(min_length=1, max_length=20)
    realised_net: Decimal = Field(allow_inf_nan=False)
    trade_closed: bool


class BrokerLegProtection(BaseModel):
    leg: int = Field(ge=3, le=5)
    stop_loss: PositivePrice
    entry: PositivePrice


class BrokerTradeProtection(BrokerEventBase):
    event: Literal["trade_protection"]
    opened_at: datetime
    source: Literal["provider", "manual", "master"]
    provider_signal_prefix: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}$")
    tp2_deal_id: int = Field(gt=0)
    stops: list[BrokerLegProtection] = Field(min_length=1, max_length=3)


class BrokerPositionUpdate(BrokerEventBase):
    event: Literal["protection_update"]
    observed_at_ms: int = Field(gt=0)
    stop_loss: OptionalPrice
    take_profit: OptionalPrice


BrokerNotice = Annotated[
    BrokerDealNotice | BrokerPositionUpdate | BrokerTradeNotice | BrokerTradeProtection, Field(discriminator="event")
]


def broker_notification(
    event: BrokerDealNotice | BrokerPositionUpdate | BrokerTradeNotice | BrokerTradeProtection, now: datetime,
    *, provider_name: str | None = None
) -> tuple[str, str, str]:
    seen = event.occurred_at
    if seen.tzinfo is None or not now - timedelta(days=1) <= seen <= now + timedelta(seconds=10):
        raise HTTPException(status_code=409, detail="broker_event_outside_delivery_window")
    if isinstance(event, (BrokerTradeNotice, BrokerTradeProtection)):
        if event.opened_at.tzinfo is None or event.opened_at > event.occurred_at:
            raise HTTPException(status_code=422, detail="invalid_broker_open_timestamp")
    if isinstance(event, BrokerTradeProtection):
        tolerance = Decimal("0.005")
        if any(p.stop_loss < p.entry - tolerance if event.side == "BUY"
               else p.stop_loss > p.entry + tolerance for p in event.stops):
            raise HTTPException(status_code=422, detail="broker_stop_not_at_entry")
        source = provider_name or "Provider"
        moved = " · ".join(f"TP {p.leg} SL {_price(p.stop_loss)}" for p in event.stops)
        return (f"owner-demo-trade-protection:700207861:{event.position_id}:{event.tp2_deal_id}",
                source, f"After TP 2: remaining stops at entry or better\n{moved}")
    if isinstance(event, BrokerTradeNotice):
        return trade_notification(event, provider_name)
    local = seen.astimezone(ZoneInfo("Europe/Sofia"))
    lines = [f"PU Prime demo master | {event.side} {event.symbol}"]
    if isinstance(event, BrokerDealNotice):
        key = f"owner-demo-deal:700207861:{event.deal_id}"
        title = "SUPER SIGNALS | MASTER ENTRY" if event.event == "entry" else "SUPER SIGNALS | MASTER EXIT"
        lines.append(f"Filled: {event.volume:f} lots at {event.price:f}")
        lines.append(f"Deal {event.deal_id} | Order {event.order_id} | Position {event.position_id}")
        if event.event == "entry":
            lines.append(f"Source: {event.source.title()}")
        else:
            lines.append(f"Reason: {event.reason.replace('_', ' ')} | Deal net P&L: ${event.realised_net:+,.2f}")
    else:
        key = f"owner-demo-protection:700207861:{event.position_id}:{event.observed_at_ms}"
        title = "SUPER SIGNALS | MASTER PROTECTION UPDATE"
        lines.append(f"Position {event.position_id}")
    if event.stop_loss is not None or event.take_profit is not None:
        stop = f"{event.stop_loss:f}" if event.stop_loss else "none"
        target = f"{event.take_profit:f}" if event.take_profit else "none"
        lines.append(f"SL {stop} | TP {target}")
    lines.append(f"Broker event: {local:%d %b %Y %H:%M:%S} Sofia")
    return key, title, "\n".join(lines)


def _price(value: Decimal | None) -> str:
    return "unavailable" if value is None else (f"{value:.2f}" if value else "not set")


def _fill_price(fills: list[BrokerTradeFill]) -> str:
    prices = [f.price for f in fills]
    return _price(prices[0]) if min(prices) == max(prices) else f"{_price(min(prices))} to {_price(max(prices))}"


def trade_notification(event: BrokerTradeNotice, provider_name: str | None) -> tuple[str, str, str]:
    fills = sorted(event.fills, key=lambda fill: (fill.leg, fill.deal_id))
    if len({f.deal_id for f in fills}) != len(fills):
        raise HTTPException(status_code=422, detail="duplicate_broker_deal")
    source = provider_name or {"provider": "Provider", "manual": "Manual", "master": "Master"}[event.source]
    market = "gold" if event.symbol.upper().split(".")[0] == "XAUUSD" else event.symbol
    if event.event == "trade_entry":
        key = f"owner-demo-trade-entry:700207861:{event.position_id}"
        title = source
        stops = sorted({f.stop_loss for f in fills if f.stop_loss})
        stop_text = " / ".join(_price(p) for p in stops) or _price(fills[0].stop_loss)
        targets = {f.leg: f.take_profit for f in fills}
        lines = [f"Trade details: {event.side.title()} {market} at {_fill_price(fills)}",
                 f"SL - {stop_text}", ""]
        lines.extend(f"TP {i} - {_price(p)}" for i, p in sorted(targets.items()))
    else:
        key = f"owner-demo-trade-exit:700207861:{min(f.deal_id for f in fills)}"
        title = source
        lines = [f"{event.side.title()} {market}"]
        for leg in sorted({f.leg for f in fills if f.reason == "take_profit"}):
            matches = [f for f in fills if f.reason == "take_profit" and f.leg == leg]
            lines.append(f"TP {leg} HIT - {_fill_price(matches)}")
        stops = [f for f in fills if f.reason == "stop_loss"]
        if stops:
            lines.append(f"SL HIT - {_fill_price(stops)}")
        other = [f for f in fills if f.reason == "broker_deal"]
        if other:
            lines.append(f"{'CLOSED' if event.trade_closed else 'PARTIAL CLOSE'} - {_fill_price(other)}")
        net = event.realised_net
        money = ("+" if net >= 0 else "-") + f"${abs(net):,.2f}"
        label = "Trade P&L" if event.trade_closed else "Trade P&L so far"
        lines.append(f"{label} - {money}")
    return key, title, "\n".join(lines)


def queue_broker_notice(event: BrokerDealNotice | BrokerPositionUpdate | BrokerTradeNotice | BrokerTradeProtection) -> dict[str, str | int | None]:
    now = datetime.now(timezone.utc)
    # Validate before making any database queries, including the timezone of provider lookup.
    key, title, body = broker_notification(event, now)
    with get_session_factory()() as session:
        payload = event.model_dump(mode="json") | {"broker_confirmed": True}
        if isinstance(event, (BrokerDealNotice, BrokerTradeNotice, BrokerTradeProtection)) and event.source == "provider" and event.provider_signal_prefix:
            opened_at = event.opened_at if isinstance(event, (BrokerTradeNotice, BrokerTradeProtection)) else event.occurred_at
            matches = session.execute(text("""
                SELECT g.id::text AS signal_id,g.provider_chat_id,g.provider_message_id,
                       COALESCE(NULLIF(s.chat_title,''),s.source_alias) AS provider_name
                FROM signals g JOIN sources s ON s.id=g.source_id
                WHERE g.id::text LIKE :prefix AND g.parser_status='accepted'
                  AND g.source_posted_at BETWEEN :start AND :finish
                LIMIT 2
            """), {
                "prefix": event.provider_signal_prefix + "-%",
                "start": opened_at - timedelta(minutes=3),
                "finish": opened_at + timedelta(seconds=5),
            }).mappings().all()
            if len(matches) == 1:
                payload.update(matches[0])
                if isinstance(event, (BrokerTradeNotice, BrokerTradeProtection)):
                    key, title, body = broker_notification(event, now, provider_name=matches[0]["provider_name"])
        if isinstance(event, BrokerTradeNotice):
            originals = session.execute(text("""
                SELECT n.event_key,d.telegram_message_id
                FROM notification_events n
                JOIN telegram_notification_deliveries d ON d.notification_id=n.id
                WHERE n.event_key=ANY(:keys) AND d.status='sent'
                  AND d.telegram_message_id IS NOT NULL
            """), {"keys": [f"owner-demo-deal:700207861:{f.deal_id}" for f in event.fills]}).mappings().all()
            if len({row["event_key"] for row in originals}) == len(event.fills):
                return {"status": "duplicate", "telegram_message_id": originals[-1]["telegram_message_id"]}
        new_id = session.execute(text("""
            INSERT INTO notification_events (
                event_key,signal_id,lifecycle_event_id,user_id,audience,kind,title,body,payload
            )
            VALUES (:key,NULL,NULL,NULL,'shared','owner_demo_broker_event',
                    :title,:body,CAST(:payload AS jsonb))
            ON CONFLICT (event_key) DO NOTHING RETURNING id
        """), {
            "key": key, "title": title, "body": body,
            "payload": json.dumps(payload),
        }).scalar_one_or_none()
        if new_id is not None:
            session.execute(text("""
                INSERT INTO telegram_notification_deliveries (notification_id,status)
                VALUES (:id,'pending') ON CONFLICT (notification_id) DO NOTHING
            """), {"id": new_id})
            session.commit()
            return {"status": "pending", "telegram_message_id": None}
        delivery = session.execute(text("""
            SELECT d.status,d.telegram_message_id,d.failure_code,d.attempt_count
            FROM notification_events n
            JOIN telegram_notification_deliveries d ON d.notification_id=n.id
            WHERE n.event_key=:key
        """), {"key": key}).mappings().one()
        if delivery["status"] == "failed" and delivery["failure_code"] == "telegram_http_429" and delivery["attempt_count"] < 3:
            retry = session.execute(text("""
                UPDATE telegram_notification_deliveries d
                SET status='pending',updated_at=now()
                FROM notification_events n
                WHERE n.id=d.notification_id AND n.event_key=:key
                  AND d.status='failed' AND d.failure_code='telegram_http_429'
                  AND d.attempt_count < 3 AND d.attempted_at <= now()-interval '2 minutes'
                RETURNING d.id
            """), {"key": key}).scalar_one_or_none()
            if retry is not None:
                session.commit()
                return {"status": "pending", "telegram_message_id": None}
            return {"status": "rate_limited", "telegram_message_id": None}
        return {"status": delivery["status"], "telegram_message_id": delivery["telegram_message_id"]}
