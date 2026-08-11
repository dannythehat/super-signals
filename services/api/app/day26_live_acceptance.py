"""One-shot live Vantage demo acceptance for Day 26 V1.

This module is never part of normal provider execution. It exists only to prove that
an accepted simple-zone XAUUSD signal can pass Days 23-26 and produce exactly three
mapped broker positions on the connected Vantage demo account.

Enable explicitly with SUPER_SIGNALS_DAY26_LIVE_ACCEPTANCE=1 during a controlled
Render deploy. The fixture uses the existing Testing source named "Test Signal
Provider", creates a clearly labelled synthetic message/signal, risks only 0.5%, and
leaves the three demo positions protected by their fixture SL/TPs as acceptance
evidence. A completed acceptance version is idempotent and will not trade again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import text

from app.db import get_session_factory
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_execution_day26_atomic import AtomicDay26Mt5ExecutionService
from app.mt5_read_service_day23 import Day23LiveState, Day23Mt5ReadService, Day23ReadError

logger = logging.getLogger(__name__)

_ACCEPTANCE_VERSION = "day26-v1-live-2026-08-11"
_TEST_SOURCE_ALIAS = "Test Signal Provider"
_READ_ATTEMPTS = 5
_READ_RETRY_SECONDS = 3.0


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _credential_keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    keys = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not keys:
        raise RuntimeError("day26_live_acceptance_missing_broker_keys")
    return keys


def _completed(session_factory) -> bool:
    with session_factory() as session:
        return bool(
            session.execute(
                text(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM audit_events
                        WHERE event_type = 'mt5.day26_live_acceptance_completed'
                          AND payload ->> 'acceptance_version' = :version
                    )
                    """
                ),
                {"version": _ACCEPTANCE_VERSION},
            ).scalar_one()
        )


def _owner_and_source(session_factory) -> tuple[UUID, UUID, int]:
    with session_factory() as session:
        account = session.execute(
            text(
                """
                SELECT owner_user_id
                FROM mt5_accounts
                WHERE account_environment = 'demo'
                  AND status = 'connected'
                ORDER BY updated_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
        if account is None:
            raise RuntimeError("day26_live_acceptance_demo_not_connected")

        source = session.execute(
            text(
                """
                SELECT id, chat_id
                FROM sources
                WHERE source_alias = :alias
                  AND status IN ('testing', 'live')
                LIMIT 1
                """
            ),
            {"alias": _TEST_SOURCE_ALIAS},
        ).mappings().first()
        if source is None:
            raise RuntimeError("day26_live_acceptance_test_source_missing")

        return account["owner_user_id"], source["id"], int(source["chat_id"])


async def _read_live_state_with_retry(
    reader: Day23Mt5ReadService,
    owner_id: UUID,
) -> Day23LiveState:
    """Retry only transient pre-fixture Day 23 reads; never retry a trade submission."""
    last_error: Day23ReadError | None = None
    for attempt in range(1, _READ_ATTEMPTS + 1):
        try:
            return await reader.read_owner_live_state(owner_id)
        except Day23ReadError as exc:
            last_error = exc
            if not exc.retryable or attempt == _READ_ATTEMPTS:
                break
            logger.warning(
                "Day 26 live acceptance Day23 read retry attempt=%s/%s code=%s",
                attempt,
                _READ_ATTEMPTS,
                exc.code,
            )
            await asyncio.sleep(_READ_RETRY_SECONDS)

    assert last_error is not None
    raise RuntimeError(f"day26_live_acceptance_day23:{last_error.code}") from last_error


def _insert_fixture(
    session_factory,
    *,
    source_id: UUID,
    provider_chat_id: int,
    entry_low: Decimal,
    entry_high: Decimal,
    stop_loss: Decimal,
    take_profits: tuple[Decimal, Decimal, Decimal],
) -> UUID:
    # Negative timestamp IDs cannot collide with genuine Telegram message IDs in this
    # Testing source and make acceptance fixtures visually obvious in the ledger.
    telegram_message_id = -int(time.time() * 1000)
    posted_at = datetime.now(UTC)
    text_body = (
        f"[DAY26 V1 ACCEPTANCE FIXTURE] BUY XAUUSD {entry_high}/{entry_low}\n"
        f"SL {stop_loss}\n"
        f"TP1 {take_profits[0]}\nTP2 {take_profits[1]}\nTP3 {take_profits[2]}"
    )
    content_hash = hashlib.sha256(text_body.encode("utf-8")).hexdigest()
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
                )
                VALUES (
                    :source_id, :telegram_message_id, :raw_text,
                    CAST(:raw_payload AS jsonb), :posted_at, 'parsed', :content_sha256
                )
                RETURNING id
                """
            ),
            {
                "source_id": source_id,
                "telegram_message_id": telegram_message_id,
                "raw_text": text_body,
                "raw_payload": json.dumps(
                    {
                        "acceptance_fixture": True,
                        "acceptance_version": _ACCEPTANCE_VERSION,
                    }
                ),
                "posted_at": posted_at,
                "content_sha256": content_hash,
            },
        ).scalar_one()

        signal_id = session.execute(
            text(
                """
                INSERT INTO signals (
                    source_message_id, source_id, provider_chat_id,
                    provider_message_id, source_revision_index, source_posted_at,
                    signal_fingerprint, symbol, side, order_type, entry_low,
                    entry_high, stop_loss, take_profits, has_open_runner,
                    parser_status, skip_reason, risk_multiplier, original_text
                )
                VALUES (
                    :source_message_id, :source_id, :provider_chat_id,
                    :provider_message_id, 0, :source_posted_at,
                    :fingerprint, 'XAUUSD', 'BUY', 'market', :entry_low,
                    :entry_high, :stop_loss, CAST(:take_profits AS jsonb), false,
                    'accepted', NULL, 1, :original_text
                )
                RETURNING id
                """
            ),
            {
                "source_message_id": message_id,
                "source_id": source_id,
                "provider_chat_id": provider_chat_id,
                "provider_message_id": telegram_message_id,
                "source_posted_at": posted_at,
                "fingerprint": fingerprint,
                "entry_low": entry_low,
                "entry_high": entry_high,
                "stop_loss": stop_loss,
                "take_profits": json.dumps([str(value) for value in take_profits]),
                "original_text": text_body,
            },
        ).scalar_one()
        session.commit()
        return signal_id


def _record_completed(session_factory, *, owner_id: UUID, signal_id: UUID, result) -> None:
    with session_factory() as session:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    actor_user_id, event_type, entity_type, entity_id, payload
                )
                VALUES (
                    :owner_id,
                    'mt5.day26_live_acceptance_completed',
                    'signal',
                    :signal_id,
                    CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "owner_id": owner_id,
                "signal_id": signal_id,
                "payload": json.dumps(
                    {
                        "acceptance_version": _ACCEPTANCE_VERSION,
                        "account_environment": "demo",
                        "symbol": result.symbol,
                        "side": result.side,
                        "execution_entry": str(result.signal_entry_price),
                        "position_count": len(result.positions),
                        "broker_position_ids": [
                            item.broker_position_id for item in result.positions
                        ],
                        "broker_order_ids": [item.broker_order_id for item in result.positions],
                        "volumes": [str(item.volume) for item in result.positions],
                        "take_profits": [
                            str(item.take_profit) if item.take_profit is not None else None
                            for item in result.positions
                        ],
                        "risk_percent": str(result.effective_risk_percent),
                    }
                ),
            },
        )
        session.commit()


async def run_day26_live_acceptance() -> None:
    session_factory = get_session_factory()
    if _completed(session_factory):
        print(f"Day 26 live acceptance already completed: {_ACCEPTANCE_VERSION}")
        return

    owner_id, source_id, provider_chat_id = _owner_and_source(session_factory)
    cipher = MetaApiTokenCipher(_credential_keys())
    read_gateway = MetaApiReadGateway()

    reader = Day23Mt5ReadService(
        session_factory=session_factory,
        cipher=cipher,
        gateway=read_gateway,
    )
    live = await _read_live_state_with_retry(reader, owner_id)

    if not live.execution_ready:
        raise RuntimeError(
            f"day26_live_acceptance_not_ready:{live.execution_block_reason or 'unknown'}"
        )

    ask = _money(Decimal(str(live.price.ask)))
    # A deliberately narrow explicit zone avoids moving-market exact-price rejection
    # while preserving the V1 rule that the live BUY ask must be inside provider bounds.
    entry_low = _money(ask - Decimal("0.10"))
    entry_high = _money(ask + Decimal("0.10"))
    stop_loss = _money(entry_low - Decimal("0.50"))
    take_profits = (
        _money(entry_high + Decimal("0.50")),
        _money(entry_high + Decimal("1.00")),
        _money(entry_high + Decimal("1.50")),
    )

    signal_id = _insert_fixture(
        session_factory,
        source_id=source_id,
        provider_chat_id=provider_chat_id,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        take_profits=take_profits,
    )

    service = AtomicDay26Mt5ExecutionService(
        session_factory=session_factory,
        cipher=cipher,
        read_gateway=read_gateway,
        margin_gateway=MetaApiMarginGateway(),
        trade_gateway=MetaApiTradeGateway(),
        zone_wait_seconds=300.0,
        zone_poll_seconds=1.0,
    )
    try:
        result = await service.execute_owner_demo_signal(
            owner_user_id=owner_id,
            signal_id=signal_id,
            risk_percent="0.5",
            double_lot_approved=False,
        )
    except Day26ExecutionError as exc:
        raise RuntimeError(f"day26_live_acceptance_execution:{exc.code}") from exc

    if len(result.positions) != 3:
        raise RuntimeError("day26_live_acceptance_position_count")
    if any(not item.broker_position_id or not item.broker_order_id for item in result.positions):
        raise RuntimeError("day26_live_acceptance_mapping_missing")

    _record_completed(
        session_factory,
        owner_id=owner_id,
        signal_id=signal_id,
        result=result,
    )
    print(
        "Day 26 LIVE Vantage demo acceptance PASSED "
        f"signal={signal_id} positions=3 entry={result.signal_entry_price}"
    )


__all__ = ["run_day26_live_acceptance"]
