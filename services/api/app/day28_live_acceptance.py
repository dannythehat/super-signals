"""One-shot Day 28 live acceptance through the real Telegram listener and Vantage demo.

This helper is inert unless ``SUPER_SIGNALS_DAY28_LIVE_ACCEPTANCE=1``.  It sends one
fresh complete XAUUSD zone signal into the explicitly configured Test Signal Provider,
waits for the normal listener -> AI/V1 -> Day 26 route to create mapped broker
positions, then sends an explicit reply ``Close all`` and verifies the normal Day 27
management route closes those same mapped positions.

It never calls the Day 26/27 execution services directly.  The Telegram route itself is
what must pass.  The hook is removed from startup after acceptance.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import get_settings
from app.metaapi_read_gateway import MetaApiReadGateway
from app.models import AuditEvent
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_read_service_day23 import Day23Mt5ReadService
from app.telegram_crypto import TelegramSessionCipher

logger = logging.getLogger(__name__)
_ACCEPTANCE_VERSION = "day28-live-2026-08-12"


def _broker_cipher() -> MetaApiTokenCipher:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    keys = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not keys:
        raise RuntimeError("day28_acceptance_broker_keys_missing")
    return MetaApiTokenCipher(keys)


def _quantize_floor(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)


def _quantize_ceil(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def _source_connection(
    session_factory: sessionmaker[Session],
    source_id: UUID,
) -> tuple[int, bytes]:
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT s.chat_id, ta.session_ciphertext
                FROM sources AS s
                JOIN telegram_accounts AS ta ON ta.id = s.telegram_account_id
                WHERE s.id = :source_id
                  AND s.status = 'testing'
                  AND ta.status = 'connected'
                LIMIT 1
                """
            ),
            {"source_id": source_id},
        ).mappings().first()
    if row is None:
        raise RuntimeError("day28_acceptance_test_source_not_connected")
    return int(row["chat_id"]), bytes(row["session_ciphertext"])


def _find_signal(
    session_factory: sessionmaker[Session],
    *,
    source_id: UUID,
    provider_message_id: int,
):
    with session_factory() as session:
        return session.execute(
            text(
                """
                SELECT id, parser_status, risk_multiplier, take_profits
                FROM signals
                WHERE source_id = :source_id
                  AND provider_message_id = :provider_message_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {
                "source_id": source_id,
                "provider_message_id": provider_message_id,
            },
        ).mappings().first()


def _position_rows(
    session_factory: sessionmaker[Session],
    *,
    signal_id: UUID,
    owner_user_id: UUID,
) -> list[dict]:
    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text(
                    """
                    SELECT tp_index, planned_risk_percent, volume, status,
                           broker_order_id, broker_position_id, close_reason
                    FROM positions
                    WHERE signal_id = :signal_id
                      AND user_id = :user_id
                    ORDER BY tp_index
                    """
                ),
                {"signal_id": signal_id, "user_id": owner_user_id},
            ).mappings().all()
        ]


def _route_success_count(
    session_factory: sessionmaker[Session],
    *,
    signal_id: UUID,
    route: str,
) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM audit_events
                    WHERE event_type = 'mt5.day28_route_success'
                      AND entity_id = :signal_id
                      AND payload ->> 'route' = :route
                    """
                ),
                {"signal_id": signal_id, "route": route},
            ).scalar_one()
        )


def _record_acceptance(
    session_factory: sessionmaker[Session],
    *,
    owner_user_id: UUID,
    signal_id: UUID,
    provider_message_id: int,
    close_message_id: int,
    rows: list[dict],
) -> None:
    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_user_id,
                event_type="mt5.day28_live_acceptance_completed",
                entity_type="signal",
                entity_id=signal_id,
                payload={
                    "acceptance_version": _ACCEPTANCE_VERSION,
                    "telegram_new_trade_message_id": provider_message_id,
                    "telegram_close_message_id": close_message_id,
                    "risk_percent": "1",
                    "double_lot_approved": True,
                    "signal_position_count": len(rows),
                    "all_positions_broker_mapped": all(
                        bool(str(row.get("broker_position_id") or "").strip()) for row in rows
                    ),
                    "all_positions_closed": all(row.get("status") == "closed" for row in rows),
                    "new_trade_route_success_count": _route_success_count(
                        session_factory, signal_id=signal_id, route="new_trade"
                    ),
                    "management_route_success_count": _route_success_count(
                        session_factory, signal_id=signal_id, route="trade_update"
                    ),
                    "automatic_retry": False,
                    "duplicate_broker_action": False,
                },
            )
        )
        session.commit()


async def run_day28_live_acceptance(
    *,
    session_factory: sessionmaker[Session],
) -> None:
    """Send and verify one real Telegram -> Vantage demo round trip."""
    if os.getenv("SUPER_SIGNALS_DAY28_LIVE_ACCEPTANCE", "").strip() != "1":
        return

    owner_raw = os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip()
    source_raw = os.getenv("SUPER_SIGNALS_DAY28_SOURCE_IDS", "").strip()
    try:
        owner_user_id = UUID(owner_raw)
        source_ids = [UUID(value.strip()) for value in source_raw.split(",") if value.strip()]
    except ValueError:
        logger.error("Day 28 live acceptance skipped: invalid owner/source UUID")
        return
    if len(source_ids) != 1:
        logger.error("Day 28 live acceptance requires exactly one Test Signal Provider source")
        return
    source_id = source_ids[0]

    settings = get_settings()
    if settings.telegram_api_id is None or settings.telegram_api_hash is None:
        logger.error("Day 28 live acceptance skipped: Telegram API credentials unavailable")
        return

    try:
        # Allow the normal Day 28 listener worker to connect and register handlers first.
        await asyncio.sleep(8)

        broker_cipher = _broker_cipher()
        live = await Day23Mt5ReadService(
            session_factory=session_factory,
            cipher=broker_cipher,
            gateway=MetaApiReadGateway(),
        ).read_owner_live_state(owner_user_id)
        ask = Decimal(str(live.price.ask))

        low = _quantize_floor(ask - Decimal("0.25"))
        high = _quantize_ceil(ask + Decimal("0.25"))
        sl = _quantize_floor(low - Decimal("3.00"))
        tp1 = _quantize_ceil(high + Decimal("3.00"))
        tp2 = _quantize_ceil(high + Decimal("5.00"))
        tp3 = _quantize_ceil(high + Decimal("7.00"))
        signal_text = (
            "XAUUSD BUY\n"
            f"ENTRY: {low}-{high}\n"
            f"SL: {sl}\n"
            f"TP1: {tp1}\n"
            f"TP2: {tp2}\n"
            f"TP3: {tp3}"
        )

        chat_id, session_ciphertext = _source_connection(session_factory, source_id)
        telegram_cipher = TelegramSessionCipher(settings.telegram_session_keys)
        session_string = telegram_cipher.decrypt(session_ciphertext)
        client = TelegramClient(
            StringSession(session_string),
            settings.telegram_api_id,
            settings.telegram_api_hash,
            device_model="Super Signals Day 28 Acceptance",
            app_version="0.9",
            system_lang_code="en",
            lang_code="en",
        )
        client.session.save_entities = False
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("day28_acceptance_telegram_not_authorized")
            sent = await client.send_message(chat_id, signal_text)
            provider_message_id = int(sent.id)
            logger.info(
                "Day 28 acceptance signal sent to Test Signal Provider message_id=%d",
                provider_message_id,
            )

            signal_id: UUID | None = None
            rows: list[dict] = []
            for _ in range(40):
                await asyncio.sleep(1)
                signal = _find_signal(
                    session_factory,
                    source_id=source_id,
                    provider_message_id=provider_message_id,
                )
                if signal is None:
                    continue
                signal_id = UUID(str(signal["id"]))
                rows = _position_rows(
                    session_factory,
                    signal_id=signal_id,
                    owner_user_id=owner_user_id,
                )
                if (
                    len(rows) == 3
                    and all(row.get("status") == "open" for row in rows)
                    and all(bool(str(row.get("broker_position_id") or "").strip()) for row in rows)
                    and _route_success_count(
                        session_factory, signal_id=signal_id, route="new_trade"
                    ) == 1
                ):
                    break
            else:
                raise RuntimeError("day28_acceptance_new_trade_route_timeout")

            assert signal_id is not None
            if any(Decimal(str(row["planned_risk_percent"])) != Decimal("1") for row in rows):
                raise RuntimeError("day28_acceptance_risk_not_one_percent")

            close = await client.send_message(
                chat_id,
                "Close all",
                reply_to=provider_message_id,
            )
            close_message_id = int(close.id)
            logger.info(
                "Day 28 acceptance close reply sent message_id=%d reply_to=%d",
                close_message_id,
                provider_message_id,
            )

            for _ in range(40):
                await asyncio.sleep(1)
                rows = _position_rows(
                    session_factory,
                    signal_id=signal_id,
                    owner_user_id=owner_user_id,
                )
                if (
                    len(rows) == 3
                    and all(row.get("status") == "closed" for row in rows)
                    and _route_success_count(
                        session_factory, signal_id=signal_id, route="trade_update"
                    ) == 1
                ):
                    break
            else:
                raise RuntimeError("day28_acceptance_management_route_timeout")

            _record_acceptance(
                session_factory,
                owner_user_id=owner_user_id,
                signal_id=signal_id,
                provider_message_id=provider_message_id,
                close_message_id=close_message_id,
                rows=rows,
            )
            logger.info(
                "Day 28 LIVE Telegram-to-Vantage acceptance PASSED signal=%s positions=3 risk=1 close_all=3",
                signal_id,
            )
        finally:
            await client.disconnect()
    except Exception:
        logger.exception("Day 28 live Telegram-to-Vantage acceptance failed")
