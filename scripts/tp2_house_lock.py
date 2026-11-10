"""Broker-verified TP2 house profit lock for the one Contabo Super Signals MT5 pipeline.

Never reads provider mid-trade management instructions. Only broker-confirmed
TP2 closing deals may arm the lock; all remaining legs are protected using
their own actual MT5 opening price. No new positions, volume changes or closes.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(r"C:\ProgramData\GoldThinker\SuperSignals\Master700207861-Copier")
MASTER_MAGIC = 2601001
FOLLOWER_MAGIC = 806026
OPEN_RETCODES = {10009, 10010}
MASTER_COMMENT = re.compile(r"SS_([0-9a-fA-F]{8})_([1-5])of([1-5])\Z")
AUDIT = ROOT / "tp2-house-lock-audit.jsonl"


def stamp():
    return datetime.now(timezone.utc).isoformat()


def report(event, **fields):
    # No credentials, prices in reports only; preserve append-only local evidence.
    payload = {"utc": stamp(), "event": event, **fields}
    try:
        with AUDIT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    except OSError:
        pass
    print(json.dumps(payload, sort_keys=True, default=str), flush=True)


def master_groups(db):
    rows = db.execute(
        "SELECT signal_id,status,detail FROM executions WHERE status IN ('submitted','ambiguous')"
    ).fetchall()
    groups = []
    for signal_id, status, raw in rows:
        try:
            payload = json.loads(raw)
            proof = payload["proof"]
            legs = int(proof["legs"])
            results = payload["results"]
            if not (3 <= legs <= 5 and len(results) >= 3):
                continue
            mapping = {}
            for index, result in enumerate(results, 1):
                if int(result.get("retcode", 0)) not in OPEN_RETCODES:
                    break
                order = int(result["order"])
                deal = int(result["deal"])
                if order <= 0 or deal <= 0:
                    break
                mapping[index] = (order, deal)
            if 2 not in mapping or not any(i >= 3 for i in mapping):
                continue
            groups.append({
                "id": str(signal_id), "short": str(signal_id).split("-")[0],
                "legs": legs, "mapping": mapping, "status": status
            })
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return groups


def needs_stop_upgrade(side, current_sl, entry, point):
    """Never weaken an existing stop. Tolerate broker price tick precision."""
    if not (math.isfinite(float(entry)) and float(entry) > 0):
        raise ValueError("invalid_verified_entry")
    sl = float(current_sl or 0)
    tolerance = max(0.00000001, float(point) * 0.25)
    if int(side) == 0:  # BUY
        return sl <= 0 or sl < float(entry) - tolerance
    if int(side) == 1:  # SELL
        return sl <= 0 or sl > float(entry) + tolerance
    raise ValueError("unknown_position_direction")


def confirmed_tp2(mt5, opening_deal_id, history, live_positions, expected_magic):
    """Only complete, broker-TP closed TP2 positions with a matched opening deal."""
    opening = next((d for d in history if int(d.ticket) == int(opening_deal_id)), None)
    if opening is None or int(opening.magic) != expected_magic or int(opening.entry) != int(mt5.DEAL_ENTRY_IN):
        return None
    pid = int(opening.position_id)
    if pid <= 0 or any(int(getattr(p, "identifier", p.ticket)) == pid for p in live_positions):
        return None
    take_profit = [
        d for d in history
        if int(d.position_id) == pid
        and int(d.entry) in (int(mt5.DEAL_ENTRY_OUT), int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3)))
        and int(d.reason) == int(mt5.DEAL_REASON_TP)
        and int(d.type) in (0, 1)
    ]
    if not take_profit:
        return None
    close = max(take_profit, key=lambda d: int(d.time))
    # Direction is confirmed by the opening deal and closing deal price.
    if int(opening.type) == 0 and float(close.price) <= float(opening.price):
        return None
    if int(opening.type) == 1 and float(close.price) >= float(opening.price):
        return None
    return {"opening_deal": int(opening.ticket), "position_id": pid,
            "closing_deal": int(close.ticket), "broker_time": int(close.time)}


def _lock_schema(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS profit_locks "
        "(signal_id TEXT PRIMARY KEY, trigger TEXT NOT NULL, evidence_json TEXT NOT NULL, "
        "created_utc TEXT NOT NULL)"
    )
    db.commit()


def _master_locks(db):
    try:
        return {str(row[0]) for row in db.execute("SELECT signal_id FROM profit_locks")}
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return set()
        raise


def _apply_entry_stop(mt5, position, expected_magic, expected_comment):
    if int(position.magic) != expected_magic:
        raise RuntimeError("house_lock_magic_mismatch")
    if str(position.comment or "") != expected_comment:
        raise RuntimeError("house_lock_comment_mismatch")
    symbol = mt5.symbol_info(position.symbol)
    if symbol is None:
        raise RuntimeError("house_lock_symbol_unavailable")
    point = float(symbol.point)
    entry = round(float(position.price_open), int(symbol.digits))
    if not needs_stop_upgrade(position.type, position.sl, entry, point):
        return False
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        raise RuntimeError("house_lock_quote_unavailable")
    distance = max(float(symbol.trade_stops_level), float(symbol.trade_freeze_level)) * point
    # Never substitute an unsafe stop or close a trade if BE is temporarily frozen.
    if int(position.type) == 0 and float(tick.bid) - entry < distance:
        raise RuntimeError("house_lock_broker_distance_or_price_reversal")
    if int(position.type) == 1 and entry - float(tick.ask) < distance:
        raise RuntimeError("house_lock_broker_distance_or_price_reversal")
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": int(position.ticket),
        "symbol": str(position.symbol),
        "sl": entry,
        "tp": float(position.tp or 0),
        "magic": expected_magic,
    }
    response = mt5.order_send(request)
    if response is None or int(response.retcode) != int(mt5.TRADE_RETCODE_DONE):
        raise RuntimeError("house_lock_broker_modify_rejected_" + str(
            getattr(response, "retcode", mt5.last_error()[0])
        ))
    verified = mt5.positions_get(ticket=int(position.ticket))
    if verified is None:
        raise RuntimeError("house_lock_broker_readback_failed")
    if not verified:
        # Trade could have legitimately closed during the stop modification.
        return True
    actual = verified[0]
    if int(actual.magic) != expected_magic or abs(float(actual.sl) - entry) > point * 0.51:
        raise RuntimeError("house_lock_stop_readback_mismatch")
    return True


def scan_master(db, mt5):
    """Call while the existing authenticated master MT5 session is connected."""
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError("house_lock_master_positions_unavailable")
    remaining = {
        int(getattr(p, "identifier", p.ticket)): p
        for p in positions
        if int(p.magic) == MASTER_MAGIC
        and (match := MASTER_COMMENT.fullmatch(str(p.comment or ""))) is not None
        and int(match.group(2)) >= 3
    }
    if not remaining:
        return
    _lock_schema(db)
    locked = _master_locks(db)
    groups = master_groups(db)
    now = datetime.now(timezone.utc)
    # Existing source trades are short-lived, but include 30 days to survive downtime.
    history = mt5.history_deals_get(now - timedelta(days=30), now + timedelta(hours=6))
    if history is None:
        raise RuntimeError("house_lock_master_history_unavailable")
    deals = list(history)
    by_ticket = {int(d.ticket): d for d in deals}
    for group in groups:
        mapped = {}
        for index, (order_id, deal_id) in group["mapping"].items():
            if index < 3:
                continue
            opening = by_ticket.get(deal_id)
            if opening is None or int(opening.magic) != MASTER_MAGIC or int(opening.entry) != int(mt5.DEAL_ENTRY_IN):
                continue
            pos = remaining.get(int(opening.position_id))
            if pos is not None:
                mapped[index] = pos
        if not mapped:
            continue
        trigger = group["id"] in locked
        if not trigger:
            proof = confirmed_tp2(
                mt5, group["mapping"][2][1], deals, positions, MASTER_MAGIC
            )
            if proof is None:
                continue
            db.execute(
                "INSERT OR IGNORE INTO profit_locks(signal_id,trigger,evidence_json,created_utc)"
                " VALUES(?,?,?,?)",
                (group["id"], "broker_confirmed_master_tp2", json.dumps(proof), stamp())
            )
            db.commit()
            locked.add(group["id"])
            report("HOUSE_TP2_LOCK_TRIGGERED", account=700207861,
                   signal_id=group["id"], proof=proof)
        for index, pos in mapped.items():
            expected = f"SS_{group['short']}_{index}of{group['legs']}"
            try:
                changed = _apply_entry_stop(mt5, pos, MASTER_MAGIC, expected)
                if changed:
                    report("HOUSE_TP2_STOP_VERIFIED", account=700207861,
                           signal_id=group["id"], tp_index=index,
                           position=int(pos.ticket), entry=float(pos.price_open))
            except Exception as exc:
                report("HOUSE_TP2_STOP_FAILED", account=700207861,
                       signal_id=group["id"], tp_index=index,
                       position=int(pos.ticket), why=str(exc)[:150])


def _write_follower_journal(path, data):
    tmp = Path(str(path) + ".house.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def scan_follower(mt5, cfg, journal_path):
    """Call after copier.process, with expected follower terminal connected."""
    positions_raw = mt5.positions_get()
    if positions_raw is None:
        raise RuntimeError("house_lock_follower_positions_unavailable")
    positions = {int(p.ticket): p for p in positions_raw}
    db_path = ROOT / "demo-execution.sqlite3"
    with sqlite3.connect("file:" + db_path.as_posix() + "?mode=ro",
                         uri=True, timeout=5) as db:
        groups = master_groups(db)
        master_locked = _master_locks(db)
    if not groups:
        return
    path = Path(journal_path)
    if not path.is_file():
        raise RuntimeError("house_lock_follower_journal_missing")
    journal = json.loads(path.read_text(encoding="utf-8-sig"))
    if int(journal.get("account", 0)) != int(cfg["target_login"]):
        raise RuntimeError("house_lock_follower_identity_mismatch")
    records = journal.get("positions", {})
    account_locked = journal.setdefault("house_tp2_locks", {})
    # Check broker's own TP2 history only where a mapped TP3+ is actually open.
    relevant = []
    for group in groups:
        mapped = {}
        for index, (source_order, _) in group["mapping"].items():
            if index < 3:
                continue
            entry = records.get(str(source_order), {})
            if entry.get("status") != "open":
                continue
            ticket = int(entry.get("target_ticket") or 0)
            if ticket in positions:
                mapped[index] = (positions[ticket], source_order)
        if mapped:
            relevant.append((group, mapped))
    if not relevant:
        return
    need_history = any(
        group["id"] not in master_locked and group["id"] not in account_locked
        for group, mapped in relevant
    )
    history = []
    if need_history:
        now = datetime.now(timezone.utc)
        raw = mt5.history_deals_get(now - timedelta(days=30), now + timedelta(hours=6))
        if raw is None:
            raise RuntimeError("house_lock_follower_history_unavailable")
        history = list(raw)
    by_ticket = {int(d.ticket): d for d in history}
    mutated = False
    for group, mapped in relevant:
        signal_id = group["id"]
        active = signal_id in master_locked or signal_id in account_locked
        if not active:
            tp2_order = group["mapping"][2][0]
            tp2_entry = records.get(str(tp2_order), {})
            tp2_ticket = int(tp2_entry.get("target_ticket") or 0)
            if tp2_ticket and tp2_ticket not in positions:
                opening = next((
                    d for d in history
                    if int(d.position_id) == tp2_ticket
                    and int(d.entry) == int(mt5.DEAL_ENTRY_IN)
                    and int(d.magic) == FOLLOWER_MAGIC
                ), None)
                if opening is not None:
                    proof = confirmed_tp2(
                        mt5, opening.ticket, history, positions_raw, FOLLOWER_MAGIC
                    )
                    if proof is not None:
                        active = True
        if not active:
            continue
        if signal_id not in account_locked:
            account_locked[signal_id] = {
                "utc": stamp(), "source": "master_broker_tp2" if signal_id in master_locked
                else "own_broker_tp2"
            }
            mutated = True
            report("HOUSE_TP2_FOLLOWER_ARMED", account=cfg["target_login"],
                   signal_id=signal_id, reason=account_locked[signal_id]["source"])
        for index, (pos, source_order) in mapped.items():
            try:
                changed = _apply_entry_stop(
                    mt5, pos, FOLLOWER_MAGIC, "SSC" + str(source_order)
                )
                if changed:
                    report("HOUSE_TP2_STOP_VERIFIED", account=cfg["target_login"],
                           signal_id=signal_id, tp_index=index,
                           position=int(pos.ticket), entry=float(pos.price_open))
            except Exception as exc:
                report("HOUSE_TP2_STOP_FAILED", account=cfg["target_login"],
                       signal_id=signal_id, tp_index=index,
                       position=int(pos.ticket), why=str(exc)[:150])
    if mutated:
        _write_follower_journal(path, journal)


def self_test():
    assert needs_stop_upgrade(0, 95, 100, 0.01)
    assert not needs_stop_upgrade(0, 101, 100, 0.01)
    assert needs_stop_upgrade(1, 105, 100, 0.01)
    assert not needs_stop_upgrade(1, 99, 100, 0.01)
    assert not needs_stop_upgrade(0, 100, 100, 0.01)
    assert not needs_stop_upgrade(1, 100, 100, 0.01)
    from types import SimpleNamespace as N
    class Fake:
        DEAL_ENTRY_IN = 0
        DEAL_ENTRY_OUT = 1
        DEAL_ENTRY_OUT_BY = 3
        DEAL_REASON_TP = 5
    opened = N(ticket=10, magic=MASTER_MAGIC, entry=0, type=0,
               position_id=20, price=100, time=1)
    done = N(ticket=11, magic=MASTER_MAGIC, entry=1, type=1,
             position_id=20, reason=5, price=110, time=2)
    assert confirmed_tp2(Fake, 10, [opened, done], [], MASTER_MAGIC)["closing_deal"] == 11
    assert confirmed_tp2(Fake, 10, [opened, done], [N(ticket=20)], MASTER_MAGIC) is None
    assert confirmed_tp2(Fake, 10, [opened, N(**{**vars(done), "reason": 4})],
                         [], MASTER_MAGIC) is None
    print("HOUSE_TP2_PURE_TESTS_PASSED", flush=True)


if __name__ == "__main__":
    self_test()
