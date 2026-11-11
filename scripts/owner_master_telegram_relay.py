"""Read verified MT5 master events and await receipts from the existing publisher."""
from __future__ import annotations

import json
import msvcrt
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import MetaTrader5 as mt5

ROOT = Path(r"C:\ProgramData\GoldThinker\SuperSignals\Master700207861-Copier")
DB = ROOT / "demo-execution.sqlite3"
ENV = Path(r"C:\Users\Administrator\AppData\Local\SuperSignalsBridge\.env")
CUTOFF = ROOT / "owner-master-trade-group-cutover-utc.txt"
TERMINAL = r"C:\ProgramData\GoldThinker\SuperSignals\MasterConnected\MT5\terminal64.exe"
ENDPOINT = "/internal/local-bridge/v1/owner-demo-700207861-broker-publication"
HOST = "super-signals-day-8.onrender.com"
LOGIN = 700207861
SERVER = "PUPrime-Demo"
UTC = timezone.utc


def emit(event, **fields):
    print(json.dumps(dict(utc=datetime.now(UTC).isoformat(), event=event, **fields)), flush=True)


def credentials():
    values = {}
    for line in ENV.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    base = urlsplit(values.get("SUPER_SIGNALS_API_URL", ""))
    token = values.get("SUPER_SIGNALS_LOCAL_BRIDGE_TOKEN", "")
    if (base.scheme, base.netloc, base.path.rstrip("/"), base.query, base.fragment) != ("https", HOST, "", "", "") or len(token) < 32:
        raise RuntimeError("existing_bridge_credentials_unavailable")
    return "https://" + HOST + ENDPOINT, token


def read_cutoff():
    cutoff = datetime.fromisoformat(CUTOFF.read_text(encoding="ascii").strip())
    if cutoff.tzinfo is None or cutoff > datetime.now(UTC):
        raise RuntimeError("invalid_publication_cutover")
    return cutoff


def connect():
    if not mt5.initialize(path=TERMINAL, portable=True, timeout=10000):
        raise RuntimeError(f"master_init_failed_{mt5.last_error()[0]}")
    a, t = mt5.account_info(), mt5.terminal_info()
    if not a or not t or not t.connected or (a.login, a.server, a.trade_mode) != (LOGIN, SERVER, 0):
        raise RuntimeError("master_broker_identity_mismatch")


@lru_cache(maxsize=2)
def confirmed_broker_offset(day):
    import owner_demo_account_reporter as reporter
    now = datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(hours=12)
    deals = mt5.history_deals_get(now - timedelta(days=14), now + timedelta(days=1))
    if deals is None:
        raise RuntimeError("broker_clock_confirmation_unavailable")
    hours = reporter.offset_from_execution_journal(list(deals), now=now)
    return hours * 3600


def broker_offset(now):
    tick = mt5.symbol_info_tick("XAUUSD.s")
    if tick is None or not tick.time_msc:
        raise RuntimeError("master_broker_clock_unavailable")
    offset = confirmed_broker_offset(now.date().isoformat())
    age = now.timestamp() - (int(tick.time_msc) / 1000 - offset)
    if not -5 <= age <= 30:
        raise RuntimeError("master_broker_clock_stale")
    return offset


def capture(cutoff, now, known_keys=frozenset()):
    try:
        connect()
        offset = broker_offset(now)
        begin = (now - timedelta(days=1)).timestamp()
        history = mt5.history_deals_get(int(begin + offset) - 1, int(now.timestamp() + offset) + 5)
        positions = mt5.positions_get()
        if history is None or positions is None:
            raise RuntimeError(f"master_broker_read_failed_{mt5.last_error()[0]}")
        history = list(history)
        opened = {d.position_id for d in history if d.entry == 0}
        missing = {d.position_id for d in history if d.entry in (1, 3) and d.position_id not in opened
                   and d.time_msc / 1000 - offset > cutoff.timestamp()}
        for pid in missing:
            related = mt5.history_deals_get(position=int(pid))
            if related is not None:
                history.extend(related)
        return list(group_events(history, positions, offset, cutoff, now, order_protection, known_keys))
    finally:
        mt5.shutdown()


def order_protection(deal, live):
    orders = mt5.history_orders_get(ticket=int(deal.order))
    if orders:
        order = next((o for o in orders if o.ticket == deal.order), None)
        if order is not None:
            return str(order.sl), str(order.tp)
    return (str(live.sl), str(live.tp)) if live else (None, None)


def fill_payload(deal, leg, protection=(None, None)):
    stop, target = protection
    return dict(deal_id=int(deal.ticket), order_id=int(deal.order), position_id=int(deal.position_id),
                leg=leg, volume=str(deal.volume), price=str(deal.price), stop_loss=stop, take_profit=target,
                reason={4: "stop_loss", 5: "take_profit"}.get(deal.reason, "broker_deal"),
                realised_net=str(deal_net(deal)))


def deal_net(deal):
    return sum(Decimal(str(getattr(deal, field, 0) or 0)) for field in ("profit", "commission", "swap", "fee"))


def group_events(history, positions, offset, cutoff, now, protection, known_keys=frozenset()):
    unique = {d.ticket: d for d in history if d.type in (0, 1) and d.entry in (0, 1, 3)
              and min(d.ticket, d.order, d.position_id, d.volume, d.price) > 0}
    deals = sorted(unique.values(), key=lambda d: (d.time_msc, d.ticket))
    origins, groups, exits = {}, {}, {}
    live = {p.identifier: p for p in positions}
    for deal in deals:
        if deal.entry != 0 or deal.position_id in origins:
            continue
        tag = re.fullmatch(r"SS_([0-9a-fA-F]{8})_([1-5])of([1-5])", str(deal.comment or ""))
        provider = tag is not None and deal.magic == 2601001 and int(tag[2]) <= int(tag[3])
        prefix = tag[1].lower() if provider else None
        key = ("provider", prefix, deal.symbol, deal.type) if provider else ("position", deal.position_id)
        leg = int(tag[2]) if provider else 1
        total = int(tag[3]) if provider else 1
        source = "provider" if provider else ("manual" if deal.magic == 0 and deal.reason in (0, 1, 2) else "master")
        origins[deal.position_id] = (key, leg)
        groups.setdefault(key, dict(first=deal, source=source, prefix=prefix, expected=total, entries=[]))
    for deal in deals:
        origin = origins.get(deal.position_id)
        if origin:
            key, leg = origin
            if deal.entry == 0:
                groups[key]["entries"].append((deal, leg))
            else:
                exits.setdefault(key, []).append((deal, leg))

    def stamp(deal):
        return datetime.fromtimestamp(deal.time_msc / 1000 - offset, UTC)

    def eligible(deal):
        when = stamp(deal)
        return cutoff < when <= now + timedelta(seconds=5)

    for key, group in groups.items():
        first = group["first"]
        root = int(first.position_id)
        base = dict(master_login=LOGIN, server=SERVER, position_id=root,
                    symbol=str(first.symbol), side="BUY" if first.type == 0 else "SELL",
                    opened_at=stamp(first).isoformat(), source=group["source"],
                    provider_signal_prefix=group["prefix"])
        entries = group["entries"]
        entry_key = f"trade-entry:{root}"
        complete = len({leg for _, leg in entries}) == group["expected"]
        old_entry = all(f"deal:{d.ticket}" in known_keys for d, _ in entries)
        if not old_entry and entry_key not in known_keys and eligible(first) and (complete or now - stamp(first) >= timedelta(seconds=4)):
            yield entry_key, base | dict(
                event="trade_entry", occurred_at=stamp(first).isoformat(),
                fills=[fill_payload(d, leg, protection(d, live.get(d.position_id))) for d, leg in entries if eligible(d)],
                realised_net=str(sum(deal_net(d) for d, _ in entries)), trade_closed=False,
            )

        group_exits = exits.get(key, [])
        batches = []
        for deal, leg in group_exits:
            if not eligible(deal):
                continue
            if not batches or deal.time_msc - batches[-1][0][0].time_msc > 2000:
                batches.append([])
            batches[-1].append((deal, leg))
        for batch in batches:
            last = batch[-1][0]
            exit_key = f"trade-exit:{min(d.ticket for d, _ in batch)}"
            if (exit_key in known_keys or all(f"deal:{d.ticket}" in known_keys for d, _ in batch)
                    or now - stamp(last) < timedelta(seconds=2)):
                continue
            relevant = [d for d, _ in entries + group_exits if (d.time_msc, d.ticket) <= (last.time_msc, last.ticket)]
            volume = sum(Decimal(str(d.volume)) * (1 if d.entry == 0 else -1) for d in relevant)
            yield exit_key, base | dict(
                event="trade_exit", occurred_at=stamp(last).isoformat(),
                fills=[fill_payload(d, leg) for d, leg in batch],
                realised_net=str(sum(deal_net(d) for d in relevant)),
                trade_closed=complete and volume <= 0,
            )

        tp2 = next((d for d, leg in group_exits if leg == 2 and d.reason == 5 and d.position_id not in live and eligible(d)), None)
        move_key = f"trade-protection-tp2:{root}"
        if tp2 is None or move_key in known_keys or now - stamp(tp2) < timedelta(seconds=2):
            continue
        remaining = {d.position_id: (leg, live[d.position_id]) for d, leg in entries
                     if leg > 2 and d.position_id in live}
        if remaining and all(p.sl > 0 and (p.sl >= p.price_open - .005 if first.type == 0 else
                                           p.sl <= p.price_open + .005) for _, p in remaining.values()):
            yield move_key, base | dict(
                event="trade_protection", occurred_at=now.isoformat(), tp2_deal_id=int(tp2.ticket),
                stops=[dict(leg=leg, stop_loss=str(p.sl), entry=str(p.price_open))
                       for leg, p in sorted(remaining.values(), key=lambda x: x[0])],
            )


def record(db, cutoff, now):
    known = frozenset(key for key, in db.execute(
        "SELECT event_key FROM owner_master_broker_publication"
    ))
    events = list(capture(cutoff, now, known))
    with db:
        db.executemany(
            "INSERT OR IGNORE INTO owner_master_broker_publication (event_key,payload) VALUES (?,?)",
            ((key, json.dumps(payload)) for key, payload in events),
        )


def verified_trade_snapshot(notices):
    import owner_demo_account_reporter as reporter

    deal_ids = {fill["deal_id"] for notice in notices for fill in notice.get("fills", ())}
    deal_ids.update(notice[key] for notice in notices for key in ("deal_id", "tp2_deal_id") if key in notice)
    _, _, snapshot, _ = reporter.verified_snapshot(required_deals=deal_ids)
    return snapshot


def deliver(db, url, token):
    rows = db.execute(
        """SELECT event_key,payload FROM owner_master_broker_publication
           WHERE status IN ('new','pending','sending') ORDER BY attempts,rowid LIMIT 20"""
    ).fetchall()
    if not rows:
        return
    notices = [(key, json.loads(body)) for key, body in rows]
    try:
        snapshot = verified_trade_snapshot([notice for _, notice in notices])
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        emit("broker_trade_account_snapshot_retry", reason=str(exc)[:100])
        return
    for key, notice in notices:
        body = json.dumps(notice | {"account_snapshot": snapshot})
        with db:
            db.execute("UPDATE owner_master_broker_publication SET attempts=attempts+1 WHERE event_key=?", (key,))
        try:
            req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                         headers={"Authorization": "Bearer " + token,
                                                  "Content-Type": "application/json",
                                                  "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=6) as response:
                receipt = json.load(response)
            state = receipt.get("status")
            message_id = receipt.get("telegram_message_id")
            if state == "sent":
                if not isinstance(message_id, int) or message_id <= 0:
                    raise RuntimeError("sent_receipt_missing_telegram_message_id")
                emit("broker_event_telegram_delivered", key=key, telegram_message_id=message_id)
            elif state == "duplicate":
                emit("duplicate_broker_event_suppressed", key=key, original_message_id=message_id)
            elif state == "failed":
                emit("broker_event_telegram_delivery_failed", key=key)
            elif state == "rate_limited":
                state = "pending"
            elif state not in ("pending", "sending"):
                raise RuntimeError("unexpected_publisher_receipt")
            with db:
                db.execute("UPDATE owner_master_broker_publication SET status=?,telegram_message_id=? WHERE event_key=?",
                           (state, message_id, key))
        except urllib.error.HTTPError as exc:
            emit("broker_event_publisher_http_error", key=key, code=exc.code)
            if exc.code in (409, 422):
                with db:
                    db.execute("UPDATE owner_master_broker_publication SET status='rejected' WHERE event_key=?", (key,))
        except (OSError, ValueError, RuntimeError) as exc:
            emit("broker_event_publisher_retry", key=key, reason=type(exc).__name__)
            break


def main():
    url, token = credentials()
    cutoff = read_cutoff()
    if "--check" in sys.argv:
        capture(cutoff, datetime.now(UTC))
        snapshot = verified_trade_snapshot([])
        print("VERIFIED_MASTER_BROKER_PUBLICATION_READ_ONLY", flush=True)
        print(json.dumps({key: snapshot[key] for key in (
            "balance", "realised_net", "month_realised_net", "reporting_start_utc", "captured_at"
        )}), flush=True)
        return
    with (ROOT / "owner-master-telegram-worker.lock").open("a+b") as lock:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        db = sqlite3.connect(DB, timeout=5)
        try:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS owner_master_broker_publication (
                event_key TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
                attempts INTEGER NOT NULL DEFAULT 0, telegram_message_id INTEGER
              );
            """)
            emit("owner_master_broker_publication_started", cutoff=cutoff.isoformat())
            last_broker_error = None
            while True:
                now = datetime.now(UTC)
                try:
                    record(db, cutoff, now)
                    if last_broker_error is not None:
                        emit("broker_publication_capture_recovered")
                    last_broker_error = None
                except Exception as exc:
                    reason = str(exc)[:100]
                    if reason != last_broker_error:
                        emit("broker_publication_capture_error", reason=reason)
                    last_broker_error = reason
                deliver(db, url, token)
                time.sleep(3)
        finally:
            db.close()


if __name__ == "__main__":
    main()
