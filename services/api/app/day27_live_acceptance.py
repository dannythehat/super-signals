"""One-shot Vantage demo acceptance for Day 27 management execution.

This temporary module proves broker-side follow-up execution on an isolated Test Signal
Provider fixture. It opens three small Day 26 demo positions, externally closes TP1 to
simulate a user/manual MT5 close, then proves Day 27 reconciles that leg without
reopening it, moves surviving legs to break-even, changes TP2, and closes the remainder.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.day26_live_acceptance import (
    _credential_keys,
    _money,
    _owner_and_source,
    _read_live_state_with_retry,
)
from app.db import get_session_factory
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.mt5_management_day27 import Day27ManagementError, Day27Mt5ManagementService
from app.mt5_read_service_day23 import Day23Mt5ReadService

_ACCEPTANCE_VERSION = "day27-live-2026-08-12"


def _completed(session_factory) -> bool:
    with session_factory() as session:
        return bool(
            session.execute(
                text(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM audit_events
                        WHERE event_type = 'mt5.day27_live_acceptance_completed'
                          AND payload ->> 'acceptance_version' = :version
                    )
                    """
                ),
                {"version": _ACCEPTANCE_VERSION},
            ).scalar_one()
        )


def _insert_signal_fixture(
    session_factory,
    *,
    source_id: UUID,
    provider_chat_id: int,
    entry_low: Decimal,
    entry_high: Decimal,
    stop_loss: Decimal,
    take_profits: tuple[Decimal, Decimal, Decimal],
) -> UUID:
    telegram_message_id = -int(time.time() * 1000)
    posted_at = datetime.now(UTC)
    body = (
        f"[DAY27 MANAGEMENT ACCEPTANCE FIXTURE] BUY XAUUSD {entry_high}/{entry_low}\n"
        f"SL {stop_loss}\nTP1 {take_profits[0]}\nTP2 {take_profits[1]}\nTP3 {take_profits[2]}"
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(
        f"{_ACCEPTANCE_VERSION}|{telegram_message_id}|{body}".encode("utf-8")
    ).hexdigest()
    with session_factory() as session:
        message_id = session.execute(
            text(
                """
                INSERT INTO messages (
                    source_id, telegram_message_id, raw_text, raw_payload,
                    posted_at, ingestion_status, content_sha256
                ) VALUES (
                    :source, :telegram_id, :body, CAST(:payload AS jsonb),
                    :posted_at, 'parsed', :digest
                ) RETURNING id
                """
            ),
            {
                "source": source_id,
                "telegram_id": telegram_message_id,
                "body": body,
                "payload": json.dumps(
                    {"acceptance_fixture": True, "acceptance_version": _ACCEPTANCE_VERSION}
                ),
                "posted_at": posted_at,
                "digest": digest,
            },
        ).scalar_one()
        signal_id = session.execute(
            text(
                """
                INSERT INTO signals (
                    source_message_id, source_id, provider_chat_id, provider_message_id,
                    source_revision_index, source_posted_at, signal_fingerprint, symbol,
                    side, order_type, entry_low, entry_high, stop_loss, take_profits,
                    has_open_runner, parser_status, skip_reason, risk_multiplier, original_text
                ) VALUES (
                    :message_id, :source, :chat, :telegram_id, 0, :posted_at,
                    :fingerprint, 'XAUUSD', 'BUY', 'market', :entry_low, :entry_high,
                    :stop_loss, CAST(:tps AS jsonb), false, 'accepted', NULL, 1, :body
                ) RETURNING id
                """
            ),
            {
                "message_id": message_id,
                "source": source_id,
                "chat": provider_chat_id,
                "telegram_id": telegram_message_id,
                "posted_at": posted_at,
                "fingerprint": fingerprint,
                "entry_low": entry_low,
                "entry_high": entry_high,
                "stop_loss": stop_loss,
                "tps": json.dumps([str(item) for item in take_profits]),
                "body": body,
            },
        ).scalar_one()
        session.commit()
        return signal_id


def _insert_management_event(
    session_factory,
    *,
    signal_id: UUID,
    suffix: str,
    actions: list[dict[str, str | None]],
) -> UUID:
    event_key = f"day27-live-acceptance:{_ACCEPTANCE_VERSION}:{signal_id}:{suffix}"
    first = actions[0]
    revised = {
        "update_type": first["type"],
        "update_target": first.get("target"),
        "update_value": first.get("value"),
        "management_actions": actions,
    }
    with session_factory() as session:
        event_id = session.execute(
            text(
                """
                INSERT INTO signal_lifecycle_events (
                    signal_id, source_message_id, source_revision_index, event_type,
                    event_key, origin, rendered_text, aggregate_result, occurred_at
                ) VALUES (
                    :signal_id, NULL, 0, 'provider_update', :event_key,
                    'provider_update', :rendered, CAST(:aggregate AS jsonb), :occurred_at
                ) RETURNING id
                """
            ),
            {
                "signal_id": signal_id,
                "event_key": event_key,
                "rendered": f"DAY27 ACCEPTANCE {suffix}",
                "aggregate": json.dumps(
                    {"ai_supervisor": True, "revised_instruction": revised}
                ),
                "occurred_at": datetime.now(UTC),
            },
        ).scalar_one()
        session.commit()
        return event_id


def _load_account_token(session_factory, owner_id: UUID, cipher: MetaApiTokenCipher) -> tuple[str, str]:
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT metaapi_account_id, metaapi_token_ciphertext
                FROM mt5_accounts
                WHERE owner_user_id=:owner AND account_environment='demo' AND status='connected'
                LIMIT 1
                """
            ),
            {"owner": owner_id},
        ).mappings().one()
    return str(row["metaapi_account_id"]), cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))


async def _wait_until_position_gone(
    read_gateway: MetaApiReadGateway,
    *,
    token: str,
    account_id: str,
    region: str,
    position_id: str,
) -> None:
    for _ in range(15):
        positions = await read_gateway.read_positions(
            token=token, account_id=account_id, region=region
        )
        ids = {str(item.get("id") or "") for item in positions}
        if position_id not in ids:
            return
        await asyncio.sleep(1.0)
    raise RuntimeError("day27_live_acceptance_manual_close_not_visible")


def _record_completed(
    session_factory,
    *,
    owner_id: UUID,
    signal_id: UUID,
    opened,
    manual_position_id: str,
    be_result,
    tp_result,
    close_result,
) -> None:
    with session_factory() as session:
        rows = session.execute(
            text(
                """
                SELECT tp_index, status, close_reason, broker_position_id, stop_loss, take_profit
                FROM positions WHERE signal_id=:signal ORDER BY tp_index
                """
            ),
            {"signal": signal_id},
        ).mappings().all()
        payload = {
            "acceptance_version": _ACCEPTANCE_VERSION,
            "account_environment": "demo",
            "signal_id": str(signal_id),
            "opened_position_ids": [item.broker_position_id for item in opened.positions],
            "manual_closed_position_id": manual_position_id,
            "be_external_reconciled": be_result.external_positions_reconciled,
            "be_positions_modified": be_result.positions_modified,
            "tp_positions_modified": tp_result.positions_modified,
            "close_positions_closed": close_result.positions_closed,
            "manual_position_reopen_attempted": False,
            "final_positions": [
                {
                    "tp_index": int(row["tp_index"]),
                    "status": str(row["status"]),
                    "close_reason": row["close_reason"],
                    "broker_position_id": row["broker_position_id"],
                    "stop_loss": str(row["stop_loss"]) if row["stop_loss"] is not None else None,
                    "take_profit": str(row["take_profit"]) if row["take_profit"] is not None else None,
                }
                for row in rows
            ],
        }
        session.execute(
            text(
                """
                INSERT INTO audit_events (actor_user_id, event_type, entity_type, entity_id, payload)
                VALUES (:owner, 'mt5.day27_live_acceptance_completed', 'signal', :signal,
                        CAST(:payload AS jsonb))
                """
            ),
            {
                "owner": owner_id,
                "signal": signal_id,
                "payload": json.dumps(payload),
            },
        )
        session.commit()


async def run_day27_live_acceptance() -> None:
    session_factory = get_session_factory()
    if _completed(session_factory):
        print(f"Day 27 live acceptance already completed: {_ACCEPTANCE_VERSION}")
        return

    owner_id, source_id, provider_chat_id = _owner_and_source(session_factory)
    cipher = MetaApiTokenCipher(_credential_keys())
    read_gateway = MetaApiReadGateway()
    trade_gateway = MetaApiTradeGateway()
    reader = Day23Mt5ReadService(
        session_factory=session_factory,
        cipher=cipher,
        gateway=read_gateway,
    )
    live = await _read_live_state_with_retry(reader, owner_id)
    if not live.execution_ready or live.price.ask is None:
        raise RuntimeError("day27_live_acceptance_price_not_ready")

    ask = _money(Decimal(str(live.price.ask)))
    entry_low = _money(ask - Decimal("0.10"))
    entry_high = _money(ask + Decimal("0.10"))
    stop_loss = _money(entry_low - Decimal("4.00"))
    take_profits = (
        _money(entry_high + Decimal("20.00")),
        _money(entry_high + Decimal("30.00")),
        _money(entry_high + Decimal("40.00")),
    )
    signal_id = _insert_signal_fixture(
        session_factory,
        source_id=source_id,
        provider_chat_id=provider_chat_id,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        take_profits=take_profits,
    )

    day26 = AtomicDay26Mt5ExecutionService(
        session_factory=session_factory,
        cipher=cipher,
        read_gateway=read_gateway,
        margin_gateway=MetaApiMarginGateway(),
        trade_gateway=trade_gateway,
        zone_wait_seconds=300.0,
        zone_poll_seconds=1.0,
    )
    try:
        opened = await day26.execute_owner_demo_signal(
            owner_user_id=owner_id,
            signal_id=signal_id,
            risk_percent="0.5",
            double_lot_approved=False,
        )
    except Day26ExecutionError as exc:
        raise RuntimeError(f"day27_live_acceptance_open:{exc.code}") from exc
    if len(opened.positions) != 3:
        raise RuntimeError("day27_live_acceptance_open_count")

    account_id, token = _load_account_token(session_factory, owner_id, cipher)
    manual_position_id = opened.positions[0].broker_position_id
    await trade_gateway.close_position(
        token=token,
        account_id=account_id,
        region=live.region,
        position_id=manual_position_id,
    )
    await _wait_until_position_gone(
        read_gateway,
        token=token,
        account_id=account_id,
        region=live.region,
        position_id=manual_position_id,
    )

    manager = Day27Mt5ManagementService(
        session_factory=session_factory,
        cipher=cipher,
        read_gateway=read_gateway,
        trade_gateway=trade_gateway,
    )
    be_event = _insert_management_event(
        session_factory,
        signal_id=signal_id,
        suffix="break-even",
        actions=[{"type": "move_to_break_even", "target": "all", "value": None}],
    )
    try:
        be_result = await manager.execute_owner_demo_event(
            owner_user_id=owner_id, lifecycle_event_id=be_event
        )
    except Day27ManagementError as exc:
        raise RuntimeError(f"day27_live_acceptance_be:{exc.code}") from exc
    if be_result.external_positions_reconciled != 1 or be_result.positions_modified != 2:
        raise RuntimeError("day27_live_acceptance_no_reopen_gate")

    new_tp2 = _money(take_profits[1] + Decimal("5.00"))
    tp_event = _insert_management_event(
        session_factory,
        signal_id=signal_id,
        suffix="tp2-change",
        actions=[{"type": "edit_take_profit", "target": "TP2", "value": str(new_tp2)}],
    )
    tp_result = await manager.execute_owner_demo_event(
        owner_user_id=owner_id, lifecycle_event_id=tp_event
    )
    if tp_result.positions_modified != 1:
        raise RuntimeError("day27_live_acceptance_tp_change")

    close_event = _insert_management_event(
        session_factory,
        signal_id=signal_id,
        suffix="close-all",
        actions=[{"type": "close", "target": "all", "value": None}],
    )
    close_result = await manager.execute_owner_demo_event(
        owner_user_id=owner_id, lifecycle_event_id=close_event
    )
    if close_result.positions_closed != 2:
        raise RuntimeError("day27_live_acceptance_close_remaining")

    # Idempotent re-run of the same lifecycle event must not submit broker actions again.
    repeated = await manager.execute_owner_demo_event(
        owner_user_id=owner_id, lifecycle_event_id=close_event
    )
    if not repeated.already_applied:
        raise RuntimeError("day27_live_acceptance_idempotency")

    _record_completed(
        session_factory,
        owner_id=owner_id,
        signal_id=signal_id,
        opened=opened,
        manual_position_id=manual_position_id,
        be_result=be_result,
        tp_result=tp_result,
        close_result=close_result,
    )
    print(
        "Day 27 LIVE Vantage demo acceptance PASSED "
        f"signal={signal_id} manual_closed={manual_position_id} "
        "manual_reopen=0 be_survivors=2 tp2_modified=1 close_remaining=2"
    )


__all__ = ["run_day27_live_acceptance"]
