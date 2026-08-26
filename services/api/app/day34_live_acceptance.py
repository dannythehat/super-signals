"""One-shot Day 34 end-to-end acceptance on the isolated Vantage demo account.

This is deliberately NOT a normal runtime trading path. It exists only for controlled
Day 34 acceptance and refuses to run unless all safety preconditions hold:

* explicit env opt-in;
* exact configured Day 34 reference owner;
* connected Vantage DEMO account;
* Testing source named ``Test Signal Provider``;
* zero broker positions and zero mapped local open positions before the fixture;
* active Day 34 broker-settlement manager;
* configured live AI supervisor.

The probe creates one clearly labelled synthetic canonical Signal, proves it is NOT
published before broker execution, opens three small 0.5%-risk demo TP legs through the
already-passed Day 26 service, proves the root + same Live Trades Board update, gives the
real Day 34 AI a same-source Active Trade Watch and asks it to interpret ``BE now``, then
leaves the provider silent and closes only the three immutable broker IDs opened by the
fixture. The real Day 34 settlement manager must discover that broker settlement, create
the plain-English result, retire Active Trade Watch, update the SAME board back to zero
and publish exactly once. No trade mutation is retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.ai_lifecycle_bridge import AiLifecycleBridge
from app.ai_message_supervisor_day34 import Day34OpenAiMessageSupervisor
from app.ai_source_aware_pipeline import SourceAwareAiMessagePipeline
from app.broker_settlement_day34 import Day34BrokerSettlementManager
from app.config import get_settings
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
from app.mt5_read_service_day23 import Day23Mt5ReadService

logger = logging.getLogger(__name__)

_ACCEPTANCE_VERSION = "day34-core-live-2026-08-12-v1"
_TEST_SOURCE_ALIAS = "Test Signal Provider"
_WAIT_SECONDS = 45


def _audit_exists(session_factory, event_type: str) -> bool:
    with session_factory() as session:
        return bool(
            session.execute(
                text(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM audit_events
                        WHERE event_type=:event_type
                          AND payload ->> 'acceptance_version'=:version
                    )
                    """
                ),
                {"event_type": event_type, "version": _ACCEPTANCE_VERSION},
            ).scalar_one()
        )


def _fixture_signal_id(session_factory) -> UUID | None:
    with session_factory() as session:
        value = session.execute(
            text(
                """
                SELECT entity_id
                FROM audit_events
                WHERE event_type='mt5.day34_live_acceptance_fixture_created'
                  AND payload ->> 'acceptance_version'=:version
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"version": _ACCEPTANCE_VERSION},
        ).scalar_one_or_none()
    return UUID(str(value)) if value is not None else None


def _record_audit(
    session_factory,
    *,
    owner_id: UUID,
    event_type: str,
    signal_id: UUID | None,
    payload: dict[str, Any],
) -> None:
    body = {"acceptance_version": _ACCEPTANCE_VERSION, **payload}
    with session_factory() as session:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    actor_user_id, event_type, entity_type, entity_id, payload
                ) VALUES (
                    :owner, :event_type,
                    CASE WHEN :signal_id IS NULL THEN 'day34_acceptance' ELSE 'signal' END,
                    :signal_id,
                    CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "owner": owner_id,
                "event_type": event_type,
                "signal_id": signal_id,
                "payload": json.dumps(body),
            },
        )
        session.commit()


def _assert_preconditions(session_factory, owner_id: UUID, source_id: UUID) -> None:
    expected_raw = os.getenv("SUPER_SIGNALS_DAY34_REFERENCE_USER_ID", "").strip()
    if not expected_raw:
        raise RuntimeError("day34_live_acceptance_reference_owner_missing")
    try:
        expected = UUID(expected_raw)
    except ValueError as exc:
        raise RuntimeError("day34_live_acceptance_reference_owner_invalid") from exc
    if expected != owner_id:
        raise RuntimeError("day34_live_acceptance_wrong_owner")

    with session_factory() as session:
        account = session.execute(
            text(
                """
                SELECT account_environment, status
                FROM mt5_accounts
                WHERE owner_user_id=:owner
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {"owner": owner_id},
        ).mappings().one()
        if str(account["account_environment"]) != "demo" or str(account["status"]) != "connected":
            raise RuntimeError("day34_live_acceptance_demo_guard_failed")

        source = session.execute(
            text(
                """
                SELECT source_alias, status
                FROM sources
                WHERE id=:source
                """
            ),
            {"source": source_id},
        ).mappings().one()
        if str(source["source_alias"] or "") != _TEST_SOURCE_ALIAS:
            raise RuntimeError("day34_live_acceptance_test_source_guard_failed")
        if str(source["status"]) not in {"testing", "live"}:
            raise RuntimeError("day34_live_acceptance_test_source_not_listenable")

        local_open = int(
            session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM positions
                    WHERE user_id=:owner
                      AND status='open'
                      AND broker_position_id IS NOT NULL
                    """
                ),
                {"owner": owner_id},
            ).scalar_one()
        )
        if local_open:
            raise RuntimeError("day34_live_acceptance_existing_mapped_open_positions")


def _insert_fixture(
    session_factory,
    *,
    owner_id: UUID,
    source_id: UUID,
    provider_chat_id: int,
    entry_low: Decimal,
    entry_high: Decimal,
    stop_loss: Decimal,
    take_profits: tuple[Decimal, Decimal, Decimal],
) -> UUID:
    existing = _fixture_signal_id(session_factory)
    if existing is not None:
        return existing

    telegram_message_id = -int(time.time() * 1000)
    posted_at = datetime.now(UTC)
    body = (
        f"[DAY34 ACCEPTANCE FIXTURE] BUY XAUUSD {entry_high}/{entry_low}\n"
        f"SL {stop_loss}\n"
        f"TP1 {take_profits[0]}\nTP2 {take_profits[1]}\nTP3 {take_profits[2]}"
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(
        (
            f"{_ACCEPTANCE_VERSION}|{telegram_message_id}|BUY|XAUUSD|"
            f"{entry_low}|{entry_high}|{stop_loss}|"
            + "|".join(str(value) for value in take_profits)
        ).encode("utf-8")
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
                    {
                        "acceptance_fixture": True,
                        "acceptance_version": _ACCEPTANCE_VERSION,
                    }
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
                "tps": json.dumps([str(value) for value in take_profits]),
                "body": body,
            },
        ).scalar_one()
        session.commit()

    _record_audit(
        session_factory,
        owner_id=owner_id,
        event_type="mt5.day34_live_acceptance_fixture_created",
        signal_id=signal_id,
        payload={
            "account_environment": "demo",
            "source_alias": _TEST_SOURCE_ALIAS,
            "provider_message_id": telegram_message_id,
            "broker_trade_action_created": False,
        },
    )
    return signal_id


def _root_row(session_factory, signal_id: UUID) -> Any | None:
    with session_factory() as session:
        return session.execute(
            text(
                """
                SELECT status, telegram_message_id, sent_at, created_at,
                       reply_to_telegram_message_id
                FROM telegram_publications
                WHERE signal_id=:signal
                  AND publication_kind='signal_created'
                  AND lifecycle_event_id IS NULL
                LIMIT 1
                """
            ),
            {"signal": signal_id},
        ).mappings().first()


def _board_row(session_factory) -> Any:
    with session_factory() as session:
        return session.execute(
            text(
                """
                SELECT telegram_message_id, rendered_text, status, pinned_at,
                       failure_code, failure_reason, attempt_count
                FROM telegram_live_board_state
                WHERE id=1
                """
            )
        ).mappings().one()


def _result_row(session_factory, signal_id: UUID) -> Any | None:
    with session_factory() as session:
        return session.execute(
            text(
                """
                SELECT id, event_type, rendered_text, pips, aggregate_result, occurred_at
                FROM signal_lifecycle_events
                WHERE signal_id=:signal
                  AND event_type LIKE 'broker_result_%'
                ORDER BY occurred_at DESC, created_at DESC
                LIMIT 1
                """
            ),
            {"signal": signal_id},
        ).mappings().first()


def _publication_for_event(session_factory, event_id: UUID) -> Any | None:
    with session_factory() as session:
        return session.execute(
            text(
                """
                SELECT status, telegram_message_id, reply_to_telegram_message_id,
                       rendered_text, attempt_count
                FROM telegram_publications
                WHERE lifecycle_event_id=:event
                LIMIT 1
                """
            ),
            {"event": event_id},
        ).mappings().first()


def _counts(session_factory, signal_id: UUID, result_event_id: UUID) -> dict[str, int]:
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM signal_lifecycle_events
                     WHERE signal_id=:signal AND event_type LIKE 'broker_result_%') AS results,
                    (SELECT COUNT(*) FROM telegram_publications
                     WHERE lifecycle_event_id=:event) AS result_publications,
                    (SELECT COUNT(*) FROM notification_events
                     WHERE event_key='lifecycle:' || CAST(:event AS text)) AS notifications,
                    (SELECT COUNT(*) FROM signal_lifecycle_events
                     WHERE signal_id=:signal AND event_type='broker_position_settled') AS leg_events
                """
            ),
            {"signal": signal_id, "event": result_event_id},
        ).mappings().one()
    return {key: int(row[key]) for key in row.keys()}


async def _wait_for(label: str, predicate, *, timeout: int = _WAIT_SECONDS) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    last: Any = None
    while asyncio.get_running_loop().time() < deadline:
        last = predicate()
        if last:
            return last
        await asyncio.sleep(1.0)
    raise RuntimeError(f"day34_live_acceptance_timeout:{label}:{last!r}")


def _load_account_token(
    session_factory,
    owner_id: UUID,
    cipher: MetaApiTokenCipher,
) -> tuple[str, str]:
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT metaapi_account_id, metaapi_token_ciphertext
                FROM mt5_accounts
                WHERE owner_user_id=:owner
                  AND account_environment='demo'
                  AND status='connected'
                LIMIT 1
                """
            ),
            {"owner": owner_id},
        ).mappings().one()
    return str(row["metaapi_account_id"]), cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))


async def _wait_positions_gone(
    read_gateway: MetaApiReadGateway,
    *,
    token: str,
    account_id: str,
    region: str,
    position_ids: set[str],
) -> None:
    for _ in range(30):
        rows = await read_gateway.read_positions(
            token=token,
            account_id=account_id,
            region=region,
        )
        current = {str(row.get("id") or "") for row in rows}
        if not (position_ids & current):
            return
        await asyncio.sleep(1.0)
    raise RuntimeError("day34_live_acceptance_broker_close_not_visible")


async def run_day34_live_acceptance(
    *,
    settlement_manager: Day34BrokerSettlementManager | None,
) -> None:
    """Run the one-shot guarded Day 34 core integration acceptance."""
    session_factory = get_session_factory()
    owner_id, source_id, provider_chat_id = _owner_and_source(session_factory)

    if _audit_exists(session_factory, "mt5.day34_live_acceptance_core_completed"):
        logger.info("Day 34 live acceptance already completed: %s", _ACCEPTANCE_VERSION)
        return
    if _audit_exists(session_factory, "mt5.day34_live_acceptance_started"):
        raise RuntimeError("day34_live_acceptance_previous_run_incomplete")
    if settlement_manager is None:
        raise RuntimeError("day34_live_acceptance_settlement_watch_required")

    settings = get_settings()
    if not settings.ai_supervisor_enabled or not settings.ai_supervisor_api_key:
        raise RuntimeError("day34_live_acceptance_ai_supervisor_required")

    _assert_preconditions(session_factory, owner_id, source_id)
    cipher = MetaApiTokenCipher(_credential_keys())
    read_gateway = MetaApiReadGateway()
    trade_gateway = MetaApiTradeGateway()
    reader = Day23Mt5ReadService(
        session_factory=session_factory,
        cipher=cipher,
        gateway=read_gateway,
    )
    live = await _read_live_state_with_retry(reader, owner_id)
    if live.positions:
        raise RuntimeError("day34_live_acceptance_existing_broker_positions")
    if not live.execution_ready or live.price.ask is None:
        raise RuntimeError(
            f"day34_live_acceptance_price_not_ready:{live.execution_block_reason or 'unknown'}"
        )

    initial_board = _board_row(session_factory)
    initial_board_id = initial_board["telegram_message_id"]
    if initial_board_id is None:
        raise RuntimeError("day34_live_acceptance_board_missing")

    ask = _money(Decimal(str(live.price.ask)))
    entry_low = _money(ask - Decimal("0.30"))
    entry_high = _money(ask + Decimal("0.30"))
    stop_loss = _money(entry_low - Decimal("4.00"))
    take_profits = (
        _money(entry_high + Decimal("20.00")),
        _money(entry_high + Decimal("30.00")),
        _money(entry_high + Decimal("40.00")),
    )
    signal_id = _insert_fixture(
        session_factory,
        owner_id=owner_id,
        source_id=source_id,
        provider_chat_id=provider_chat_id,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        take_profits=take_profits,
    )

    # Give the real Day 34 publisher at least one poll before broker execution. The
    # Signal must remain invisible to members until Day 26 confirms placement.
    await asyncio.sleep(4.0)
    pre_root = _root_row(session_factory, signal_id)
    if pre_root is not None and (
        str(pre_root["status"]) == "sent" or pre_root["telegram_message_id"] is not None
    ):
        raise RuntimeError("day34_live_acceptance_root_published_before_execution")

    _record_audit(
        session_factory,
        owner_id=owner_id,
        event_type="mt5.day34_live_acceptance_started",
        signal_id=signal_id,
        payload={
            "account_environment": "demo",
            "risk_percent": "0.5",
            "initial_board_message_id": int(initial_board_id),
            "root_visible_before_execution": False,
        },
    )

    executor = AtomicDay26Mt5ExecutionService(
        session_factory=session_factory,
        cipher=cipher,
        read_gateway=read_gateway,
        margin_gateway=MetaApiMarginGateway(),
        trade_gateway=trade_gateway,
        zone_wait_seconds=300.0,
        zone_poll_seconds=1.0,
    )
    try:
        opened = await executor.execute_owner_demo_signal(
            owner_user_id=owner_id,
            signal_id=signal_id,
            risk_percent="0.5",
            double_lot_approved=False,
        )
    except Day26ExecutionError as exc:
        raise RuntimeError(f"day34_live_acceptance_open:{exc.code}") from exc
    if len(opened.positions) != 3:
        raise RuntimeError("day34_live_acceptance_open_count")

    root = await _wait_for(
        "execution_first_root",
        lambda: (
            row
            if (row := _root_row(session_factory, signal_id)) is not None
            and str(row["status"]) == "sent"
            and row["telegram_message_id"] is not None
            else None
        ),
    )
    with session_factory() as session:
        execution_at = session.execute(
            text(
                """
                SELECT MIN(created_at)
                FROM audit_events
                WHERE entity_type='signal'
                  AND entity_id=:signal
                  AND event_type='mt5.day26_execution_success'
                """
            ),
            {"signal": signal_id},
        ).scalar_one()
    if execution_at is None or root["sent_at"] is None or root["sent_at"] < execution_at:
        raise RuntimeError("day34_live_acceptance_execution_first_ordering_failed")

    board_open = await _wait_for(
        "live_board_open",
        lambda: (
            row
            if (row := _board_row(session_factory))["telegram_message_id"] == initial_board_id
            and "OPEN 1" in str(row["rendered_text"] or "")
            and "XAUUSD BUY" in str(row["rendered_text"] or "")
            else None
        ),
    )

    # Prove the real AI receives broker-backed context and understands terse management.
    supervisor = Day34OpenAiMessageSupervisor(
        api_key=settings.ai_supervisor_api_key,
        model=settings.ai_supervisor_model,
        timeout_seconds=settings.ai_supervisor_timeout_seconds,
    )
    pipeline = SourceAwareAiMessagePipeline(
        session_factory=session_factory,
        supervisor=supervisor,
    )
    active_context = pipeline._active_trade_context(source_id=source_id)  # noqa: SLF001
    current = [item for item in active_context if item.get("signal_id") == str(signal_id)]
    if len(current) != 1:
        raise RuntimeError("day34_live_acceptance_active_context_missing")
    ai_decision = supervisor.decide_with_active_context(
        raw_text="BE now",
        source_status="testing",
        source_name=_TEST_SOURCE_ALIAS,
        active_trade_context=active_context,
        recent_source_messages=[],
    )
    if (
        ai_decision.decision != "trade_update"
        or ai_decision.action != "apply_update"
        or ai_decision.extracted.get("update_type") != "move_to_break_even"
    ):
        raise RuntimeError(
            "day34_live_acceptance_active_ai_failed:"
            f"{ai_decision.decision}:{ai_decision.action}:"
            f"{ai_decision.extracted.get('update_type')}"
        )

    # Prove standalone association chooses this broker-active Signal even though the
    # source contains historical Signals. No lifecycle event is created by this probe.
    with session_factory() as session:
        linked, link_reason = AiLifecycleBridge._resolve_signal(  # noqa: SLF001
            session,
            {
                "message_id": UUID(int=34),
                "source_id": source_id,
                "telegram_message_id": -34,
                "raw_text": "BE now",
                "raw_payload": {},
                "occurred_at": datetime.now(UTC),
            },
            revision_index=0,
        )
    if linked is None or linked["id"] != signal_id or link_reason != "active_broker_unique":
        raise RuntimeError(f"day34_live_acceptance_active_link_failed:{link_reason}")

    # Provider now stays silent. Close ONLY the three immutable demo broker IDs opened
    # above, without creating any provider lifecycle event. No mutation is retried.
    account_id, token = _load_account_token(session_factory, owner_id, cipher)
    broker_ids = {str(item.broker_position_id) for item in opened.positions}
    if "" in broker_ids or len(broker_ids) != 3:
        raise RuntimeError("day34_live_acceptance_mapping_missing")
    for position_id in sorted(broker_ids):
        await trade_gateway.close_position(
            token=token,
            account_id=account_id,
            region=live.region,
            position_id=position_id,
        )
    await _wait_positions_gone(
        read_gateway,
        token=token,
        account_id=account_id,
        region=live.region,
        position_ids=broker_ids,
    )

    # Drive/read the actual settlement watcher until immutable broker deals are visible.
    for _ in range(20):
        await settlement_manager.poll_once()
        if _result_row(session_factory, signal_id) is not None:
            break
        await asyncio.sleep(2.0)
    result = _result_row(session_factory, signal_id)
    if result is None:
        raise RuntimeError("day34_live_acceptance_broker_result_missing")
    rendered_result = str(result["rendered_text"] or "")
    if "XAUUSD" not in rendered_result:
        raise RuntimeError("day34_live_acceptance_result_symbol_missing")
    if "$500 example at Recommended 1%:" not in rendered_result:
        raise RuntimeError("day34_live_acceptance_model500_missing")
    if not (
        rendered_result.startswith("🎉🎉 TRADE CLOSED — WIN")
        or rendered_result.startswith("❌ TRADE CLOSED — LOSS")
        or rendered_result.startswith("➖ TRADE CLOSED — BREAK EVEN")
    ):
        raise RuntimeError("day34_live_acceptance_plain_english_result_missing")

    result_publication = await _wait_for(
        "broker_result_telegram",
        lambda: (
            row
            if (row := _publication_for_event(session_factory, result["id"])) is not None
            and str(row["status"]) == "sent"
            and row["telegram_message_id"] is not None
            else None
        ),
    )
    if int(result_publication["reply_to_telegram_message_id"] or 0) != int(
        root["telegram_message_id"]
    ):
        raise RuntimeError("day34_live_acceptance_result_not_threaded")

    board_closed = await _wait_for(
        "live_board_closed",
        lambda: (
            row
            if (row := _board_row(session_factory))["telegram_message_id"] == initial_board_id
            and "OPEN 0" in str(row["rendered_text"] or "")
            and "PENDING 0" in str(row["rendered_text"] or "")
            else None
        ),
    )

    retired = pipeline._active_trade_context(source_id=source_id)  # noqa: SLF001
    if any(item.get("signal_id") == str(signal_id) for item in retired):
        raise RuntimeError("day34_live_acceptance_active_context_not_retired")

    counts_before = _counts(session_factory, signal_id, result["id"])
    if counts_before != {
        "results": 1,
        "result_publications": 1,
        "notifications": 1,
        "leg_events": 3,
    }:
        raise RuntimeError(f"day34_live_acceptance_exactly_once_failed:{counts_before}")

    # Replay the settlement read twice. It must create no duplicate lifecycle/result,
    # notification or Telegram publication rows.
    await settlement_manager.poll_once()
    await settlement_manager.poll_once()
    await asyncio.sleep(4.0)
    counts_after = _counts(session_factory, signal_id, result["id"])
    if counts_after != counts_before:
        raise RuntimeError(
            f"day34_live_acceptance_replay_duplicate:{counts_before}:{counts_after}"
        )

    aggregate = result["aggregate_result"] if isinstance(result["aggregate_result"], dict) else {}
    final_board = _board_row(session_factory)
    _record_audit(
        session_factory,
        owner_id=owner_id,
        event_type="mt5.day34_live_acceptance_core_completed",
        signal_id=signal_id,
        payload={
            "account_environment": "demo",
            "source_alias": _TEST_SOURCE_ALIAS,
            "risk_percent": "0.5",
            "root_visible_before_execution": False,
            "execution_first_root": True,
            "root_telegram_message_id": int(root["telegram_message_id"]),
            "live_board_message_id": int(initial_board_id),
            "live_board_same_message_open": board_open["telegram_message_id"] == initial_board_id,
            "live_board_same_message_closed": board_closed["telegram_message_id"] == initial_board_id,
            "live_board_pinned": final_board["pinned_at"] is not None,
            "live_board_status": str(final_board["status"]),
            "live_board_failure_code": final_board["failure_code"],
            "ai_active_trade_context_present": True,
            "ai_terse_be_understood": True,
            "active_link_reason": link_reason,
            "active_context_retired_after_broker_settlement": True,
            "provider_result_message_required": False,
            "broker_result_event_type": str(result["event_type"]),
            "broker_result_text": rendered_result,
            "broker_result_pips": str(result["pips"]) if result["pips"] is not None else None,
            "model_500_pnl": aggregate.get("model_500_pnl"),
            "result_telegram_message_id": int(result_publication["telegram_message_id"]),
            "result_reply_to_root": True,
            "exactly_once_counts": counts_after,
            "notification_failure_can_coexist_with_trade": final_board["pinned_at"] is None,
            "real_user_balance_exposed": False,
            "real_user_pnl_exposed": False,
        },
    )
    logger.info(
        "Day 34 LIVE core acceptance PASSED signal=%s root=%s board=%s result=%s pinned=%s",
        signal_id,
        root["telegram_message_id"],
        initial_board_id,
        result["telegram_message_id"] if "telegram_message_id" in result else result["id"],
        final_board["pinned_at"] is not None,
    )


async def run_day34_live_acceptance_safely(
    *,
    settlement_manager: Day34BrokerSettlementManager | None,
) -> None:
    """Run acceptance without allowing a probe failure to take down the application."""
    session_factory = get_session_factory()
    owner_id: UUID | None = None
    try:
        owner_id, _, _ = _owner_and_source(session_factory)
        await run_day34_live_acceptance(settlement_manager=settlement_manager)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Day 34 live acceptance failed safely: %s", exc)
        if owner_id is not None:
            try:
                _record_audit(
                    session_factory,
                    owner_id=owner_id,
                    event_type="mt5.day34_live_acceptance_failed",
                    signal_id=_fixture_signal_id(session_factory),
                    payload={
                        "error": str(exc)[:500],
                        "account_environment": "demo",
                        "automatic_retry_disabled": True,
                    },
                )
            except Exception:
                logger.exception("Day 34 acceptance failure audit could not be stored")


__all__ = ["run_day34_live_acceptance_safely"]
