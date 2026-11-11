"""Read-only PU Prime DEMO master account report -> existing Super Signals Telegram outbox.

Contabo Windows only. No order_send, no follower changes, no historical signal replay.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, time as clock_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import MetaTrader5 as mt5

ROOT = Path(r"C:\ProgramData\GoldThinker\SuperSignals\Master700207861-Copier")
DB = ROOT / "demo-execution.sqlite3"
MASTER_TERMINAL = r"C:\ProgramData\GoldThinker\SuperSignals\MasterConnected\MT5\terminal64.exe"
ACCOUNT = 700207861
SERVER = "PUPrime-Demo"
ENDPOINT = "/internal/local-bridge/v1/owner-demo-700207861-account-report"


def sofia_utc_offset(instant: datetime) -> int:
    """EU daylight-saving shift is 01:00 UTC on last Sunday Mar/Oct."""
    instant = instant.astimezone(timezone.utc)
    y = instant.year
    march = datetime(y, 3, 31, tzinfo=timezone.utc)
    october = datetime(y, 10, 31, tzinfo=timezone.utc)
    march -= timedelta(days=(march.weekday() + 1) % 7)
    october -= timedelta(days=(october.weekday() + 1) % 7)
    return 3 if march + timedelta(hours=1) <= instant < october + timedelta(hours=1) else 2


def reporting_start(now: datetime) -> datetime:
    """The Super Signals accounting day runs 21:00-21:00 Europe/Sofia."""
    local = now.astimezone(timezone.utc) + timedelta(hours=sofia_utc_offset(now))
    day = local.date() if local.hour >= 21 else local.date() - timedelta(days=1)
    local_anchor_utc = datetime.combine(day, clock_time(21), tzinfo=timezone.utc)
    offset = sofia_utc_offset(local_anchor_utc - timedelta(hours=2))
    return local_anchor_utc - timedelta(hours=offset)


def month_reporting_start(start: datetime) -> datetime:
    local = start + timedelta(hours=sofia_utc_offset(start))
    first_day = (local.date() + timedelta(days=1)).replace(day=1)
    first_window = datetime.combine(first_day - timedelta(days=1), clock_time(21), tzinfo=timezone.utc)
    return first_window - timedelta(hours=sofia_utc_offset(first_window - timedelta(hours=2)))


def deal_net(deal) -> Decimal:
    return sum(Decimal(str(getattr(deal, field, 0) or 0)) for field in ("profit", "commission", "swap", "fee"))


def trading_pnl_type(deal) -> bool:
    return int(deal.type) in (0, 1, 7, 8, 9, 10, 11)


def offset_from_execution_journal(deals: list, *, now: datetime) -> int:
    """Calibrate broker recorded deal clock using already broker-confirmed master tickets.

    Never guess timezone from the Windows host. Fails shut if broker evidence differs.
    """
    indexed = {int(d.ticket): d for d in deals}
    options: list[int] = []
    with sqlite3.connect("file:" + DB.as_posix() + "?mode=ro", uri=True, timeout=5) as conn:
        rows = conn.execute(
            "SELECT detail,updated_utc FROM executions WHERE status='submitted' "
            "ORDER BY updated_utc DESC LIMIT 35"
        ).fetchall()
    for detail, updated in rows:
        stamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if stamp.tzinfo is None or now - stamp > timedelta(days=14):
            continue
        for entry in json.loads(detail).get("results", []):
            d = indexed.get(int(entry.get("deal") or 0))
            if not d:
                continue
            delta = int(d.time) - stamp.timestamp()
            rounded = round(delta / 3600)
            if abs(rounded) <= 4 and abs(delta - rounded * 3600) < 150:
                options.append(rounded)
    if not options:
        raise RuntimeError("broker_clock_cannot_be_calibrated_from_confirmed_fills")
    return Counter(options).most_common(1)[0][0]


def verified_snapshot(*, required_deals: set[int] | None = None):
    import owner_master_telegram_relay as relay

    receipt_url, token = relay.credentials()
    if not receipt_url.endswith(("/owner-demo-700207861-confirmed-execution", "/owner-demo-700207861-broker-publication")):
        raise RuntimeError("existing_bridge_api_identity_mismatch")
    url = receipt_url.rsplit("/", 1)[0] + ENDPOINT.removeprefix("/internal/local-bridge/v1")
    now = datetime.now(timezone.utc)
    start = reporting_start(now)

    if not mt5.initialize(path=MASTER_TERMINAL, portable=True, timeout=12000):
        raise RuntimeError("master_mt5_ipc_error_" + str(mt5.last_error()[0]))
    try:
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if not account or not terminal or not terminal.connected:
            raise RuntimeError("broker_demo_terminal_disconnected")
        if (
            int(account.login) != ACCOUNT
            or account.server != SERVER
            or int(account.trade_mode) != int(mt5.ACCOUNT_TRADE_MODE_DEMO)
            or account.currency != "USD"
        ):
            raise RuntimeError("unexpected_broker_account_identity")

        # Read extra 4h each side because the broker's deal ledger stamps server time.
        # Calibrate this offset from real broker-confirmed deal IDs in our journal.
        records = mt5.history_deals_get(0, int((now + timedelta(hours=4)).timestamp()))
        if records is None:
            raise RuntimeError("broker_history_unavailable_" + str(mt5.last_error()[0]))
        records = list(records)
        broker_offset = offset_from_execution_journal(records, now=now)
        timestamp = lambda deal: datetime.fromtimestamp(
            int(deal.time), timezone.utc
        ) - timedelta(hours=broker_offset)
        records = [d for d in records if timestamp(d) <= now + timedelta(seconds=15)]
        if required_deals and not required_deals.issubset(d.ticket for d in records):
            raise RuntimeError("broker_history_missing_trade_notice_deals")
        selected = [d for d in records if timestamp(d) >= start]
        month_start = month_reporting_start(start)
        month = [d for d in records if timestamp(d) >= month_start]
        trade_deals = [d for d in selected if trading_pnl_type(d)]
        cash_deals = [d for d in selected if not trading_pnl_type(d)]
        month_net = sum((deal_net(d) for d in month if trading_pnl_type(d)), Decimal(0))
        all_time_net = sum((deal_net(d) for d in records if trading_pnl_type(d)), Decimal(0))
        # Month opening capital plus net deposits and withdrawals during the month.
        opening_balance = Decimal(str(account.balance)) - sum(map(deal_net, month), Decimal(0))
        month_capital = opening_balance + sum((deal_net(d) for d in month if int(d.type) == 2), Decimal(0))
        positions = mt5.positions_get()
        if positions is None:
            raise RuntimeError("broker_positions_read_unavailable")
        captured = datetime.now(timezone.utc)
        if required_deals is not None:
            final = mt5.account_info()
            if (final is None or (final.login, final.server, final.trade_mode, final.currency, final.balance)
                    != (account.login, account.server, account.trade_mode, account.currency, account.balance)
                    or reporting_start(captured) != start):
                raise RuntimeError("broker_account_changed_during_trade_snapshot")
        payload = {
            "master_login": ACCOUNT, "server": SERVER, "trade_mode": "demo",
            "currency": account.currency, "connected": bool(terminal.connected),
            "balance": str(round(account.balance, 2)),
            "equity": str(round(account.equity, 2)),
            "floating_pnl": str(round(account.profit, 2)),
            "credit": str(round(account.credit, 2)),
            "realised_net": str(round(sum(map(deal_net, trade_deals), Decimal(0)), 2)),
            "month_realised_net": str(round(month_net, 2)),
            "month_capital": str(round(month_capital, 2)),
            "all_time_realised_net": str(round(all_time_net, 2)),
            "cash_movements": str(round(sum(map(deal_net, cash_deals), Decimal(0)), 2)),
            "closed_deal_fills": sum(1 for d in trade_deals if d.type in (0, 1) and d.entry in (1, 2, 3)),
            "open_positions": len(positions),
            "reporting_start_utc": start.isoformat(),
            "captured_at": captured.isoformat(),
        }
        return url, token, payload, broker_offset
    finally:
        mt5.shutdown()


def run(*, check_only: bool):
    if (ROOT / "STOP").exists():
        raise RuntimeError("master_maintenance_stop_active")
    url, token, payload, offset = verified_snapshot()
    if check_only:
        print(json.dumps({"status": "preflight_ok", "payload": payload, "broker_time_offset_hours": offset}), flush=True)
        return
    request = urllib.request.Request(
        url, method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                result = json.load(response)
            if result.get("status") not in (
                "queued", "unchanged", "rate_limited", "already_queued", "delivery_failed",
            ):
                raise RuntimeError("account_report_unexpected_response")
            print(json.dumps({
                "utc": datetime.now(timezone.utc).isoformat(),
                "account": ACCOUNT,
                "status": result["status"],
                "balance": payload["balance"],
                "realised_net": payload["realised_net"],
                "notification_id": result.get("notification_id"),
            }), flush=True)
            return
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError("account_report_http_" + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise RuntimeError("account_report_network_unavailable") from None
        time.sleep(0.8 * (attempt + 1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    run(check_only=args.check)
