"""Resume the existing Day 27 live fixture without opening another demo trade.

The initial live run proved creation + manual-close reconciliation, but an immediate
move-to-entry request was rejected by the broker while the stop price was not valid at
that market moment. This resume path keeps the same signal, proves a broker-valid
explicit SL modification on the two surviving mapped positions, proves one TP change,
then closes the two remaining positions and verifies lifecycle-event idempotency.
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from uuid import UUID

from sqlalchemy import text

from app.day26_live_acceptance import _credential_keys
from app.day27_live_acceptance import _insert_management_event, _load_account_token
from app.day27_live_acceptance_retry import _RetryingMetaApiReadGateway
from app.db import get_session_factory
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_management_day27 import Day27ManagementError, Day27Mt5ManagementService

_ACCEPTANCE_VERSION = "day27-live-2026-08-12"


def _latest_fixture(session_factory) -> tuple[UUID, UUID]:
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT s.id AS signal_id, a.owner_user_id
                FROM signals s
                CROSS JOIN LATERAL (
                    SELECT owner_user_id
                    FROM mt5_accounts
                    WHERE account_environment='demo' AND status='connected'
                    ORDER BY updated_at DESC
                    LIMIT 1
                ) a
                WHERE s.original_text LIKE '[DAY27 MANAGEMENT ACCEPTANCE FIXTURE]%'
                  AND EXISTS (SELECT 1 FROM positions p WHERE p.signal_id=s.id)
                  AND NOT EXISTS (
                      SELECT 1 FROM audit_events ae
                      WHERE ae.event_type='mt5.day27_live_acceptance_completed'
                        AND ae.payload ->> 'acceptance_version'=:version
                  )
                ORDER BY s.created_at DESC
                LIMIT 1
                """
            ),
            {"version": _ACCEPTANCE_VERSION},
        ).mappings().first()
    if row is None:
        raise RuntimeError("day27_live_resume_fixture_missing")
    return row["signal_id"], row["owner_user_id"]


def _local_positions(session_factory, signal_id: UUID) -> list[dict[str, object]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                """
                SELECT tp_index, status, broker_position_id, entry_price,
                       stop_loss, take_profit, close_reason
                FROM positions
                WHERE signal_id=:signal_id
                ORDER BY tp_index
                """
            ),
            {"signal_id": signal_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def _price_quantum(spec: dict[str, object]) -> Decimal:
    digits_raw = spec.get("digits")
    try:
        digits = int(digits_raw) if digits_raw is not None else 2
    except (TypeError, ValueError):
        digits = 2
    digits = max(0, min(digits, 8))
    return Decimal(1).scaleb(-digits)


def _valid_stop_price(
    *,
    side: str,
    current_price: Decimal,
    old_stop: Decimal,
    spec: dict[str, object],
) -> Decimal:
    point = Decimal(str(spec.get("point") or spec.get("tickSize") or "0.01"))
    stops_level = Decimal(str(spec.get("stopsLevel") or 0))
    minimum_distance = max(point * stops_level, Decimal("0.50"))
    quantum = _price_quantum(spec)
    if side == "BUY":
        candidate = (current_price - minimum_distance - (point * 5)).quantize(
            quantum, rounding=ROUND_DOWN
        )
        if candidate == old_stop:
            candidate = (candidate - max(point * 10, quantum)).quantize(
                quantum, rounding=ROUND_DOWN
            )
    else:
        candidate = (current_price + minimum_distance + (point * 5)).quantize(
            quantum, rounding=ROUND_UP
        )
        if candidate == old_stop:
            candidate = (candidate + max(point * 10, quantum)).quantize(
                quantum, rounding=ROUND_UP
            )
    if candidate <= 0:
        raise RuntimeError("day27_live_resume_stop_invalid")
    return candidate


def _record_completion(
    session_factory,
    *,
    owner_id: UUID,
    signal_id: UUID,
    manual_position_id: str,
    explicit_sl: Decimal,
    sl_result,
    tp_result,
    close_result,
) -> None:
    final_positions = _local_positions(session_factory, signal_id)
    payload = {
        "acceptance_version": _ACCEPTANCE_VERSION,
        "account_environment": "demo",
        "signal_id": str(signal_id),
        "manual_closed_position_id": manual_position_id,
        "manual_position_reopen_attempted": False,
        "initial_break_even_attempt": "broker_rejected",
        "explicit_sl_value": str(explicit_sl),
        "explicit_sl_positions_modified": sl_result.positions_modified,
        "tp_positions_modified": tp_result.positions_modified,
        "close_positions_closed": close_result.positions_closed,
        "close_event_idempotent": True,
        "final_positions": [
            {
                "tp_index": int(row["tp_index"]),
                "status": str(row["status"]),
                "broker_position_id": row["broker_position_id"],
                "close_reason": row["close_reason"],
                "stop_loss": str(row["stop_loss"]) if row["stop_loss"] is not None else None,
                "take_profit": str(row["take_profit"]) if row["take_profit"] is not None else None,
            }
            for row in final_positions
        ],
    }
    with session_factory() as session:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    actor_user_id, event_type, entity_type, entity_id, payload
                ) VALUES (
                    :owner, 'mt5.day27_live_acceptance_completed', 'signal', :signal,
                    CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "owner": owner_id,
                "signal": signal_id,
                "payload": json.dumps(payload),
            },
        )
        session.commit()


async def run_day27_live_acceptance_resume() -> None:
    session_factory = get_session_factory()
    signal_id, owner_id = _latest_fixture(session_factory)
    local = _local_positions(session_factory, signal_id)
    if len(local) != 3:
        raise RuntimeError("day27_live_resume_position_count")
    if not (
        local[0]["status"] == "closed"
        and local[0]["close_reason"] == "external_close"
        and local[1]["status"] == "open"
        and local[2]["status"] == "open"
    ):
        raise RuntimeError("day27_live_resume_manual_close_state")

    cipher = MetaApiTokenCipher(_credential_keys())
    account_id, token = _load_account_token(session_factory, owner_id, cipher)
    read_gateway = _RetryingMetaApiReadGateway()
    trade_gateway = MetaApiTradeGateway()
    region = await read_gateway.resolve_account_region(token=token, account_id=account_id)

    broker_positions_raw = await read_gateway.read_positions(
        token=token, account_id=account_id, region=region
    )
    broker_positions = {
        str(item.get("id") or ""): item
        for item in broker_positions_raw
        if str(item.get("id") or "")
    }
    manual_position_id = str(local[0]["broker_position_id"] or "")
    if manual_position_id in broker_positions:
        raise RuntimeError("day27_live_resume_manual_position_reappeared")
    survivor_ids = [str(local[1]["broker_position_id"]), str(local[2]["broker_position_id"])]
    if any(position_id not in broker_positions for position_id in survivor_ids):
        raise RuntimeError("day27_live_resume_survivor_missing")

    spec = await read_gateway.read_symbol_specification(
        token=token, account_id=account_id, region=region, symbol="XAUUSD"
    )
    first_broker = broker_positions[survivor_ids[0]]
    side = "BUY" if str(first_broker.get("type") or "").upper().endswith("BUY") else "SELL"
    current_price = Decimal(str(first_broker.get("currentPrice")))
    old_stop = Decimal(str(local[1]["stop_loss"]))
    explicit_sl = _valid_stop_price(
        side=side,
        current_price=current_price,
        old_stop=old_stop,
        spec=spec,
    )

    manager = Day27Mt5ManagementService(
        session_factory=session_factory,
        cipher=cipher,
        read_gateway=read_gateway,
        trade_gateway=trade_gateway,
    )

    sl_event = _insert_management_event(
        session_factory,
        signal_id=signal_id,
        suffix="resume-explicit-sl",
        actions=[{"type": "edit_stop_loss", "target": "all", "value": str(explicit_sl)}],
    )
    try:
        sl_result = await manager.execute_owner_demo_event(
            owner_user_id=owner_id,
            lifecycle_event_id=sl_event,
        )
    except Day27ManagementError as exc:
        raise RuntimeError(f"day27_live_resume_sl:{exc.code}") from exc
    if sl_result.positions_modified != 2 or sl_result.external_positions_reconciled != 0:
        raise RuntimeError("day27_live_resume_sl_result")

    local_after_sl = _local_positions(session_factory, signal_id)
    tp2_current = Decimal(str(local_after_sl[1]["take_profit"]))
    quantum = _price_quantum(spec)
    tp2_new = (tp2_current + Decimal("5.00")).quantize(quantum)
    tp_event = _insert_management_event(
        session_factory,
        signal_id=signal_id,
        suffix="resume-tp2-change",
        actions=[{"type": "edit_take_profit", "target": "TP2", "value": str(tp2_new)}],
    )
    tp_result = await manager.execute_owner_demo_event(
        owner_user_id=owner_id,
        lifecycle_event_id=tp_event,
    )
    if tp_result.positions_modified != 1:
        raise RuntimeError("day27_live_resume_tp_result")

    close_event = _insert_management_event(
        session_factory,
        signal_id=signal_id,
        suffix="resume-close-all",
        actions=[{"type": "close", "target": "all", "value": None}],
    )
    close_result = await manager.execute_owner_demo_event(
        owner_user_id=owner_id,
        lifecycle_event_id=close_event,
    )
    if close_result.positions_closed != 2:
        raise RuntimeError("day27_live_resume_close_result")

    repeated = await manager.execute_owner_demo_event(
        owner_user_id=owner_id,
        lifecycle_event_id=close_event,
    )
    if not repeated.already_applied:
        raise RuntimeError("day27_live_resume_idempotency")

    final_local = _local_positions(session_factory, signal_id)
    if any(row["status"] != "closed" for row in final_local):
        raise RuntimeError("day27_live_resume_final_local_state")

    _record_completion(
        session_factory,
        owner_id=owner_id,
        signal_id=signal_id,
        manual_position_id=manual_position_id,
        explicit_sl=explicit_sl,
        sl_result=sl_result,
        tp_result=tp_result,
        close_result=close_result,
    )
    print(
        "Day 27 LIVE Vantage demo acceptance PASSED "
        f"signal={signal_id} manual_closed={manual_position_id} manual_reopen=0 "
        f"explicit_sl_survivors=2 tp2_modified=1 close_remaining=2"
    )


__all__ = ["run_day27_live_acceptance_resume"]
