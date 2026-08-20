"""One-time operational retirement of TDC.

This module is deliberately temporary. It runs during one controlled deployment,
removes any still-active TDC broker exposure, purges TDC from mutable user-facing
trading/performance data, rebuilds the remaining provider summaries, and is then
deleted from the production tree.

Raw Telegram messages, Signals, lifecycle events, audit events and broker deals are
immutable forensic evidence. They remain internal and cannot re-enter user-facing
performance because the TDC source is permanently revoked and all of its Position /
performance outcome mappings are removed.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_engine, get_session_factory
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.performance_ledger_canonical import CanonicalPerformanceLedgerService

_TDC_CHAT_ID = -1004415242875
_MARKER = "provider.tdc_retirement_completed"
_ACTIVE_ORDER_STATES = {"ORDER_STATE_PLACED", "ORDER_STATE_PARTIAL"}


@dataclass(frozen=True, slots=True)
class AccountTarget:
    user_id: UUID
    account_id: str
    token_ciphertext: bytes
    broker_position_ids: frozenset[str]
    broker_order_ids: frozenset[str]
    broker_client_ids: frozenset[str]


def _keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not values:
        raise RuntimeError("tdc_retirement_broker_keys_missing")
    return values


def _load_targets(session: Session) -> tuple[UUID, list[AccountTarget], set[UUID]]:
    source_id = session.execute(
        text("SELECT id FROM sources WHERE chat_id=:chat_id LIMIT 1"),
        {"chat_id": _TDC_CHAT_ID},
    ).scalar_one_or_none()
    if source_id is None:
        raise RuntimeError("tdc_retirement_source_missing")

    # Revoke first. The production listener and canonical dispatcher both require
    # testing/live, so no new TDC broker intent can race the cleanup.
    session.execute(
        text("UPDATE sources SET status='revoked', updated_at=now() WHERE id=:source_id"),
        {"source_id": source_id},
    )
    session.commit()

    user_ids = set(
        session.scalars(
            text(
                """
                SELECT DISTINCT p.user_id
                FROM positions p
                JOIN signals s ON s.id=p.signal_id
                WHERE s.source_id=:source_id
                """
            ),
            {"source_id": source_id},
        ).all()
    )

    rows = session.execute(
        text(
            """
            SELECT
                p.user_id,
                a.metaapi_account_id,
                a.metaapi_token_ciphertext,
                p.broker_position_id,
                p.broker_order_id,
                p.broker_client_id
            FROM positions p
            JOIN signals s ON s.id=p.signal_id
            JOIN mt5_accounts a ON a.owner_user_id=p.user_id
            WHERE s.source_id=:source_id
              AND a.status='connected'
              AND a.metaapi_account_id IS NOT NULL
              AND a.metaapi_token_ciphertext IS NOT NULL
            """
        ),
        {"source_id": source_id},
    ).mappings().all()

    grouped: dict[tuple[UUID, str, bytes], dict[str, set[str]]] = defaultdict(
        lambda: {"positions": set(), "orders": set(), "clients": set()}
    )
    for row in rows:
        key = (
            row["user_id"],
            str(row["metaapi_account_id"]),
            bytes(row["metaapi_token_ciphertext"]),
        )
        if row["broker_position_id"]:
            grouped[key]["positions"].add(str(row["broker_position_id"]))
        if row["broker_order_id"]:
            grouped[key]["orders"].add(str(row["broker_order_id"]))
        if row["broker_client_id"]:
            grouped[key]["clients"].add(str(row["broker_client_id"]))

    targets = [
        AccountTarget(
            user_id=user_id,
            account_id=account_id,
            token_ciphertext=token_ciphertext,
            broker_position_ids=frozenset(values["positions"]),
            broker_order_ids=frozenset(values["orders"]),
            broker_client_ids=frozenset(values["clients"]),
        )
        for (user_id, account_id, token_ciphertext), values in grouped.items()
    ]
    return source_id, targets, user_ids


def _matches(item: dict[str, object], *, ids: frozenset[str], clients: frozenset[str]) -> bool:
    item_id = str(item.get("id") or "").strip()
    client_id = str(item.get("clientId") or "").strip()
    return bool((item_id and item_id in ids) or (client_id and client_id in clients))


async def _terminal_order_ids(
    read: MetaApiReadGateway,
    *,
    token: str,
    account_id: str,
    region: str,
) -> set[str]:
    """Return terminal order tickets from bounded bulk broker history."""
    end = datetime.now(UTC)
    start = end - timedelta(days=7)
    terminal: set[str] = set()
    offset = 0
    while True:
        page = await read.read_history_orders_by_time_range(
            token=token,
            account_id=account_id,
            region=region,
            start_time=start,
            end_time=end,
            offset=offset,
            limit=1000,
        )
        for item in page:
            order_id = str(item.get("id") or "").strip()
            state = str(item.get("state") or "").strip().upper()
            if order_id and state and state not in _ACTIVE_ORDER_STATES:
                terminal.add(order_id)
        if len(page) < 1000:
            break
        offset += len(page)
        if offset > 10_000:
            raise RuntimeError("tdc_retirement_history_too_large")
    return terminal


async def _remove_broker_exposure(
    target: AccountTarget,
    cipher: MetaApiTokenCipher,
) -> tuple[int, int]:
    token = cipher.decrypt(target.token_ciphertext)
    read = MetaApiReadGateway()
    trade = MetaApiTradeGateway()
    region = await read.resolve_account_region(token=token, account_id=target.account_id)

    positions = await read.read_positions(token=token, account_id=target.account_id, region=region)
    orders = await read.read_orders(token=token, account_id=target.account_id, region=region)
    terminal_order_ids = await _terminal_order_ids(
        read,
        token=token,
        account_id=target.account_id,
        region=region,
    )

    target_positions = [
        item
        for item in positions
        if _matches(item, ids=target.broker_position_ids, clients=target.broker_client_ids)
    ]
    target_orders = [
        item
        for item in orders
        if _matches(item, ids=target.broker_order_ids, clients=target.broker_client_ids)
        and str(item.get("id") or "").strip() not in terminal_order_ids
    ]

    closed = 0
    cancelled = 0
    for item in target_positions:
        broker_id = str(item.get("id") or "").strip()
        if not broker_id:
            raise RuntimeError("tdc_retirement_position_id_missing")
        await trade.close_position(
            token=token,
            account_id=target.account_id,
            region=region,
            position_id=broker_id,
        )
        closed += 1

    for item in target_orders:
        broker_id = str(item.get("id") or "").strip()
        if not broker_id:
            raise RuntimeError("tdc_retirement_order_id_missing")
        await trade.cancel_order(
            token=token,
            account_id=target.account_id,
            region=region,
            order_id=broker_id,
        )
        cancelled += 1

    # Never make the app look clean while a TDC broker position/order is still active.
    remaining_positions = await read.read_positions(
        token=token,
        account_id=target.account_id,
        region=region,
    )
    remaining_orders = await read.read_orders(
        token=token,
        account_id=target.account_id,
        region=region,
    )
    terminal_after = await _terminal_order_ids(
        read,
        token=token,
        account_id=target.account_id,
        region=region,
    )
    if any(
        _matches(item, ids=target.broker_position_ids, clients=target.broker_client_ids)
        for item in remaining_positions
    ):
        raise RuntimeError("tdc_retirement_position_still_active")
    if any(
        _matches(item, ids=target.broker_order_ids, clients=target.broker_client_ids)
        and str(item.get("id") or "").strip() not in terminal_after
        for item in remaining_orders
    ):
        raise RuntimeError("tdc_retirement_order_still_active")
    return closed, cancelled


def _purge_mutable_trading_artifacts(source_id: UUID, user_ids: set[UUID]) -> dict[str, int]:
    """Remove TDC from every mutable app/performance surface.

    Signals, lifecycle events, audits, Telegram messages and raw broker deals are
    intentionally untouched. PostgreSQL enforces append-only lifecycle evidence.
    """
    counts: dict[str, int] = {}
    with Session(get_engine()) as session:
        signal_subquery = "SELECT id FROM signals WHERE source_id=:source_id"
        lifecycle_subquery = (
            "SELECT id FROM signal_lifecycle_events WHERE signal_id IN (" + signal_subquery + ")"
        )

        counts["notifications"] = session.execute(
            text(
                "DELETE FROM notification_events "
                "WHERE signal_id IN (" + signal_subquery + ") "
                "OR lifecycle_event_id IN (" + lifecycle_subquery + ")"
            ),
            {"source_id": source_id},
        ).rowcount or 0
        counts["publications"] = session.execute(
            text(
                "DELETE FROM telegram_publications "
                "WHERE signal_id IN (" + signal_subquery + ") "
                "OR lifecycle_event_id IN (" + lifecycle_subquery + ")"
            ),
            {"source_id": source_id},
        ).rowcount or 0
        counts["outcomes"] = session.execute(
            text("DELETE FROM performance_trade_outcomes WHERE signal_id IN (" + signal_subquery + ")"),
            {"source_id": source_id},
        ).rowcount or 0

        # broker_deals.position_id is ON DELETE SET NULL, preserving immutable broker
        # evidence while severing it from the user-facing Position model.
        counts["positions"] = session.execute(
            text("DELETE FROM positions WHERE signal_id IN (" + signal_subquery + ")"),
            {"source_id": source_id},
        ).rowcount or 0

        # Summary rows are derived. They are rebuilt from the remaining providers only.
        for user_id in user_ids:
            session.execute(
                text("DELETE FROM performance_summaries WHERE user_id=:user_id"),
                {"user_id": user_id},
            )

        session.execute(
            text(
                """
                INSERT INTO audit_events(event_type,entity_type,entity_id,payload)
                VALUES (:event_type,'source',:source_id,CAST(:payload AS jsonb))
                """
            ),
            {
                "event_type": _MARKER,
                "source_id": source_id,
                "payload": (
                    '{"provider":"TDC","performance_excluded":true,'
                    '"raw_provider_evidence_preserved":true}'
                ),
            },
        )
        session.commit()
    return counts


def _rebuild_remaining_summaries(user_ids: set[UUID], cipher: MetaApiTokenCipher) -> int:
    service = CanonicalPerformanceLedgerService(
        session_factory=get_session_factory(),
        cipher=cipher,
        gateway=MetaApiReadGateway(),
    )
    rebuilt = 0
    for user_id in user_ids:
        rebuilt += service.rebuild_summaries(user_id)
    return rebuilt


async def main() -> None:
    cipher = MetaApiTokenCipher(_keys())
    with Session(get_engine()) as session:
        already_done = session.execute(
            text("SELECT 1 FROM audit_events WHERE event_type=:event_type LIMIT 1"),
            {"event_type": _MARKER},
        ).scalar_one_or_none()
        if already_done:
            print("TDC retirement already completed")
            return
        source_id, targets, user_ids = _load_targets(session)

    closed = 0
    cancelled = 0
    for target in targets:
        account_closed, account_cancelled = await _remove_broker_exposure(target, cipher)
        closed += account_closed
        cancelled += account_cancelled

    counts = _purge_mutable_trading_artifacts(source_id, user_ids)
    summaries = _rebuild_remaining_summaries(user_ids, cipher)
    print(
        "TDC retirement completed "
        f"broker_positions_closed={closed} broker_orders_cancelled={cancelled} "
        f"positions_removed={counts.get('positions', 0)} "
        f"outcomes_removed={counts.get('outcomes', 0)} "
        f"notifications_removed={counts.get('notifications', 0)} "
        f"publications_removed={counts.get('publications', 0)} "
        f"summaries_rebuilt={summaries}"
    )


if __name__ == "__main__":
    asyncio.run(main())
