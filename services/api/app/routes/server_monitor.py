"""Owner-only, read-only monitoring of existing MT5 worker snapshots.

Never queues a broker command, writes a trading record, or exposes credentials.
The authenticated owner's phone can view the same already-collected account data.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import text

from app.access_control import get_current_identity
from app.db import get_session_factory
from app.pu_master_mirror_runtime import pu_mirror_effective_mode, pu_mirror_effective_since

router = APIRouter(prefix="/owner/server-monitor", tags=["owner-server-monitor"])
OwnerIdentity = Annotated[dict[str, Any], Depends(get_current_identity)]

_OPERATIONS = frozenset({
    "read_account_information", "read_positions",
    "read_terminal_identity", "read_symbol_price",
})
_PROFILES = ("pu-prime", "super-signals")


def _owner_only(identity: dict[str, Any]) -> None:
    if identity.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Owner access required.")


def _account_snapshot(
    profile: str,
    latest: dict[tuple[str, str], dict[str, Any]],
    workers: dict[str, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    def record(operation: str) -> dict[str, Any] | None:
        return latest.get((profile, operation))

    def payload(operation: str, expected_type: type) -> Any:
        item = record(operation)
        value = item["result"] if item and item.get("status", "succeeded") == "succeeded" else None
        return value if isinstance(value, expected_type) else None

    info = payload("read_account_information", dict)
    identity = payload("read_terminal_identity", dict)
    positions = payload("read_positions", list)
    worker = workers.get(profile)
    seen_at = worker["last_seen_at"] if worker else None
    seen_age = (now - seen_at).total_seconds() if seen_at else None
    quote = payload("read_symbol_price", dict)

    clean_positions = []
    if positions is not None:
        for pos in positions[:35]:
            if not isinstance(pos, dict):
                continue
            clean_positions.append({
                "id": str(pos.get("id") or ""),
                "symbol": str(pos.get("symbol") or ""),
                "side": "BUY" if pos.get("type") == "POSITION_TYPE_BUY" else (
                    "SELL" if pos.get("type") == "POSITION_TYPE_SELL" else "OTHER"
                ),
                "volume": pos.get("volume"),
                "entry_price": pos.get("openPrice"),
                "current_price": pos.get("currentPrice"),
                "profit": pos.get("profit"),
                "stop_loss": pos.get("stopLoss"),
                "take_profit": pos.get("takeProfit"),
                "client_id": str(pos.get("clientId") or ""),
            })
    def freshness(operation: str) -> dict[str, Any]:
        item = record(operation)
        at = (item.get('completed_at') or item.get('created_at')) if item else None
        age = (now - at).total_seconds() if at else None
        expected = list if operation == 'read_positions' else dict
        if item and not isinstance(item.get('result'), expected):
            age = None
        return {'status': 'unavailable' if age is None or item.get('status', 'succeeded') != 'succeeded' else
                'fresh' if 0 <= age <= 90 else 'stale',
                'observed_at': at, 'age_seconds': age}

    position_record = record("read_positions")
    account_record = record("read_account_information")
    return {
        "profile": profile,
        "worker_name": worker.get("worker_id") if worker else None,
        "worker_online": seen_age is not None and 0 <= seen_age <= 90,
        "worker_last_seen": seen_at,
        "balance": info.get("balance") if info else None,
        "equity": info.get("equity") if info else None,
        "free_margin": info.get("freeMargin") if info else None,
        "currency": info.get("currency") if info else None,
        "trade_allowed": info.get("tradeAllowed") if info else None,
        "login": identity.get("login") if identity else None,
        "server": identity.get("server") if identity else None,
        "account_updated_at": (account_record.get("completed_at") or account_record["created_at"]) if account_record else None,
        "positions_updated_at": (position_record.get("completed_at") or position_record["created_at"]) if position_record else None,
        "positions": clean_positions if positions is not None else None,
        "positions_total": len(positions) if positions is not None else None,
        "positions_truncated": len(positions)>35 if positions is not None else False,
        "components": {op: freshness(op) for op in sorted(_OPERATIONS)},
        "broker_state": ('unavailable' if seen_age is None else
                         'disconnected' if not 0 <= seen_age <= 90 else
                         'fresh' if all(freshness(op)['status'] == 'fresh'
                                        for op in ('read_positions','read_account_information'))
                         else 'stale'),
        "gold": {
            "symbol": quote.get("symbol"),
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "updated_at": record("read_symbol_price")["created_at"],
        } if quote and record("read_symbol_price") else None,
    }


def worker_snapshots(rows, workers, now):
    """Never join one worker's heartbeat to another worker/account's prices."""
    output=[]
    for worker in workers:
        profile=str(worker["profile"])
        own=[r for r in rows if r["profile"]==profile and r.get("claimed_by")==worker["worker_id"]]
        accounts={r["account_id"] for r in own}
        latest={}
        if len(accounts)==1:
            for row in sorted(own,key=lambda r:r.get("completed_at") or r["created_at"],reverse=True):
                latest.setdefault((profile,row["operation"]),dict(row))
        snapshot=_account_snapshot(profile,latest,{profile:worker},now)
        snapshot["account_ref"]=next(iter(accounts)) if len(accounts)==1 else None
        snapshot["account_scope"]="verified" if len(accounts)==1 else "unavailable"
        output.append(snapshot)
    return output


@router.get("")
def read_server_monitor(response: Response, identity: OwnerIdentity) -> dict[str, Any]:
    _owner_only(identity)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    now = datetime.now(UTC)
    factory = get_session_factory()
    with factory() as session:
        # Narrow bounded read using the existing (profile,status,created_at)
        # index. This uses snapshots from workers, never triggers an MT5 read.
        rows = session.execute(text("""
            SELECT profile, account_id, claimed_by, operation, status, result, created_at, completed_at
            FROM local_bridge_commands
            WHERE profile IN ('pu-prime','super-signals')
              AND status IN ('succeeded','failed','ambiguous')
              AND created_at >= NOW() - INTERVAL '5 minutes'
              AND operation IN (
                'read_account_information','read_positions',
                'read_terminal_identity','read_symbol_price'
              )
            ORDER BY created_at DESC
            LIMIT 1200
        """)).mappings().all()
        worker_rows = session.execute(text("""
            SELECT profile, worker_id, last_seen_at
            FROM local_bridge_workers
            WHERE profile IN ('pu-prime','super-signals')
            ORDER BY profile, last_seen_at DESC
        """)).mappings().all()
    independent = worker_snapshots(rows,worker_rows,now)
    def legacy(profile):
        matches=[r for r in independent if r["profile"]==profile]
        return matches[0] if len(matches)==1 else _account_snapshot(profile,{}, {},now)
    return {
        "read_at": now,
        "pu_prime": legacy("pu-prime"),
        "vantage": legacy("super-signals"),
        "mirror": {
            "mode": pu_mirror_effective_mode(),
            "forward_only_since": pu_mirror_effective_since(),
            "target_login": "36394935",
        },
        "workers": independent,
        "components": {p: legacy(p)["broker_state"]
                       for p in _PROFILES},
        "source": "Latest successful MT5 worker snapshots; read only",
    }
