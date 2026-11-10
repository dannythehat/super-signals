"""Broker-verified owner demo account snapshots via the existing Telegram outbox."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from app.db import get_session_factory

SOFIA = ZoneInfo("Europe/Sofia")
ACCOUNT = 700207861


class OwnerDemoAccountReport(BaseModel):
    master_login: Literal[700207861]
    server: Literal["PUPrime-Demo"]
    trade_mode: Literal["demo"]
    currency: Literal["USD"]
    connected: Literal[True]
    balance: Decimal
    equity: Decimal
    floating_pnl: Decimal
    credit: Decimal
    realised_net: Decimal
    month_realised_net: Decimal | None = None
    month_capital: Decimal | None = None
    all_time_realised_net: Decimal | None = None
    cash_movements: Decimal
    closed_deal_fills: int = Field(ge=0, le=1000000)
    open_positions: int = Field(ge=0, le=100000)
    reporting_start_utc: datetime
    captured_at: datetime


def _money(amount: Decimal, *, signed: bool = False) -> str:
    value = amount.quantize(Decimal("0.01"))
    core = f"{chr(36)}{abs(value):,.2f}"
    return ("+" if value >= 0 else "-") + core if signed else (("-" if value < 0 else "") + core)


def reporting_day_start(instant: datetime) -> datetime:
    local = instant.astimezone(SOFIA)
    start = local.replace(hour=21, minute=0, second=0, microsecond=0)
    if local.hour < 21:
        start -= timedelta(days=1)
    return start.astimezone(timezone.utc)


def _validate(report: OwnerDemoAccountReport, now: datetime) -> datetime:
    if report.captured_at.tzinfo is None or report.reporting_start_utc.tzinfo is None:
        raise HTTPException(status_code=422, detail="timezone_required")
    captured = report.captured_at.astimezone(timezone.utc)
    if captured < now - timedelta(minutes=3) or captured > now + timedelta(seconds=10):
        raise HTTPException(status_code=409, detail="stale_or_future_broker_snapshot")
    start = reporting_day_start(captured)
    if abs((start - report.reporting_start_utc.astimezone(timezone.utc)).total_seconds()) > 2:
        raise HTTPException(status_code=409, detail="wrong_21h_sofia_reporting_window")
    amounts = (report.balance, report.equity, report.floating_pnl, report.credit,
               report.realised_net, report.cash_movements, report.month_realised_net,
               report.month_capital, report.all_time_realised_net)
    if not all(x.is_finite() for x in amounts if x is not None) or report.balance < 0 or report.equity < 0:
        raise HTTPException(status_code=422, detail="invalid_broker_account_values")
    if abs(report.equity - report.balance - report.floating_pnl - report.credit) > Decimal("0.25"):
        raise HTTPException(status_code=409, detail="broker_equity_mismatch")
    return start


def _body(report: OwnerDemoAccountReport, start: datetime) -> str:
    def pnl(value: Decimal | None) -> str:
        return _money(value, signed=True) if value is not None else "unavailable"

    monthly = pnl(report.month_realised_net)
    if report.month_realised_net is not None and report.month_capital is not None and report.month_capital > 0:
        monthly += f" ({report.month_realised_net / report.month_capital:.2%})"
    start_local = start.astimezone(SOFIA)
    end_local = start_local + timedelta(days=1)
    return "\n".join([
        f"Daily profit / loss = {pnl(report.realised_net)}",
        f"Month to date - {monthly}",
        f"All-time profit / loss - {pnl(report.all_time_realised_net)}",
        f"Total balance - {_money(report.balance)}",
        f"Daily window {start_local:%d %b %H:%M} to {end_local:%d %b %H:%M} Sofia",
    ])


def queue_owner_demo_report(report: OwnerDemoAccountReport) -> dict[str, str | int]:
    now = datetime.now(timezone.utc)
    start = _validate(report, now)
    payload = {
        "master_login": ACCOUNT, "source": "mt5_read_only", "demo": True,
        "currency": "USD", "balance": str(report.balance), "equity": str(report.equity),
        "realised_net": str(report.realised_net), "floating_pnl": str(report.floating_pnl),
        "month_realised_net": str(report.month_realised_net),
        "month_capital": str(report.month_capital),
        "all_time_realised_net": str(report.all_time_realised_net),
        "cash_movements": str(report.cash_movements), "credit": str(report.credit),
        "open_positions": report.open_positions, "closed_deal_fills": report.closed_deal_fills,
        "reporting_start_utc": start.isoformat(), "captured_at": report.captured_at.isoformat(),
    }
    stamp = report.captured_at.astimezone(timezone.utc)
    key = f"owner-demo-account:700207861:{stamp:%Y%m%dT%H%MZ}"
    with get_session_factory()() as session:
        last = session.execute(text("""
            SELECT n.created_at,n.payload,d.status
            FROM notification_events AS n
            LEFT JOIN telegram_notification_deliveries AS d ON d.notification_id=n.id
            WHERE n.kind='owner_demo_account_status'
              AND n.event_key LIKE 'owner-demo-account:700207861:%'
            ORDER BY n.created_at DESC LIMIT 1
        """)).mappings().first()
        if last:
            elapsed = now - last["created_at"].astimezone(timezone.utc)
            prior = last["payload"] or {}
            if last["status"] in ("pending", "sending") and elapsed < timedelta(minutes=12):
                return {"status": "already_queued", "account": ACCOUNT}
            if last["status"] == "failed" and elapsed < timedelta(minutes=12):
                return {"status": "delivery_failed", "account": ACCOUNT}
            if last["status"] == "sent":
                fields = ("balance", "realised_net", "month_realised_net", "month_capital",
                          "all_time_realised_net", "reporting_start_utc")
                if all(prior.get(field) == payload[field] for field in fields):
                    return {"status": "unchanged", "account": ACCOUNT}
                if elapsed < timedelta(minutes=1):
                    return {"status": "rate_limited", "account": ACCOUNT}
        new_id = session.execute(text("""
            INSERT INTO notification_events (
                event_key,signal_id,lifecycle_event_id,user_id,
                audience,kind,title,body,payload
            )
            VALUES (:key,NULL,NULL,NULL,'shared','owner_demo_account_status',
                    :title,:body,CAST(:payload AS jsonb))
            ON CONFLICT (event_key) DO NOTHING RETURNING id
        """), {
            "key": key, "title": "Super Signals | Account",
            "body": _body(report, start), "payload": json.dumps(payload),
        }).scalar_one_or_none()
        if new_id is None:
            session.rollback()
            return {"status": "already_queued", "account": ACCOUNT}
        session.execute(text("""
            INSERT INTO telegram_notification_deliveries (notification_id,status)
            VALUES (:id,'pending') ON CONFLICT (notification_id) DO NOTHING
        """), {"id": new_id})
        session.commit()
        return {"status": "queued", "account": ACCOUNT, "notification_id": str(new_id)}
