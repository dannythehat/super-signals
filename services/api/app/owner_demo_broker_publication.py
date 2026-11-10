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


class BrokerPositionUpdate(BrokerEventBase):
    event: Literal["protection_update"]
    observed_at_ms: int = Field(gt=0)
    stop_loss: OptionalPrice
    take_profit: OptionalPrice


BrokerNotice = Annotated[
    BrokerDealNotice | BrokerPositionUpdate, Field(discriminator="event")
]


def broker_notification(
    event: BrokerDealNotice | BrokerPositionUpdate, now: datetime
) -> tuple[str, str, str]:
    seen = event.occurred_at
    if seen.tzinfo is None or not now - timedelta(days=1) <= seen <= now + timedelta(seconds=10):
        raise HTTPException(status_code=409, detail="broker_event_outside_delivery_window")
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


def queue_broker_notice(event: BrokerDealNotice | BrokerPositionUpdate) -> dict[str, str | int | None]:
    key, title, body = broker_notification(event, datetime.now(timezone.utc))
    with get_session_factory()() as session:
        payload = event.model_dump(mode="json") | {"broker_confirmed": True}
        if isinstance(event, BrokerDealNotice) and event.source == "provider" and event.provider_signal_prefix:
            matches = session.execute(text("""
                SELECT id::text AS signal_id,provider_chat_id,provider_message_id
                FROM signals
                WHERE id::text LIKE :prefix AND parser_status='accepted'
                  AND source_posted_at BETWEEN :start AND :finish
                LIMIT 2
            """), {
                "prefix": event.provider_signal_prefix + "-%",
                "start": event.occurred_at - timedelta(minutes=3),
                "finish": event.occurred_at + timedelta(seconds=5),
            }).mappings().all()
            if len(matches) == 1:
                payload.update(matches[0])
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
