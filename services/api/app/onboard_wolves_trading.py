"""One-time opt-in enrollment for @WolvesTrades from an already joined reader.

Do not join Telegram channels, scrape historical trades, or change other providers.
Only a verified joined public-channel username can be enrolled. Existing source
states are never overwritten on later restarts (including a manual pause).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import get_settings
from app.db import get_session_factory
from app.models import Source, TelegramAccount
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_source_service_day14 import get_day14_telegram_source_service

_TARGET_USERNAME = "wolvestrades"


def _is_target_dialog(dialog: object) -> bool:
    entity = getattr(dialog, "entity", None)
    username = str(getattr(entity, "username", "") or "").casefold().lstrip("@")
    return bool(getattr(dialog, "is_channel", False)) and username == _TARGET_USERNAME


async def _find_joined_chat(session_string: str, api_id: int, api_hash: str) -> int | None:
    client = TelegramClient(
        StringSession(session_string), api_id, api_hash,
        device_model="Super Signals Provider Setup",
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        async for dialog in client.iter_dialogs():
            if _is_target_dialog(dialog):
                return int(dialog.id)
        return None
    finally:
        if client.is_connected():
            await client.disconnect()


async def onboard() -> None:
    settings = get_settings()
    if not settings.telegram_api_id or not settings.telegram_api_hash:
        print("WOLVES_SETUP telegram reader not configured", flush=True)
        return
    cipher = TelegramSessionCipher(settings.telegram_session_keys)
    service = get_day14_telegram_source_service()
    factory = get_session_factory()

    with factory() as session:
        accounts = [
            (a.id, a.owner_user_id, a.session_ciphertext)
            for a in session.scalars(
                select(TelegramAccount)
                .where(TelegramAccount.status == "connected")
                .order_by(TelegramAccount.created_at.asc())
            ).all()
        ]

    for account_id, owner_id, encrypted_session in accounts:
        try:
            telegram_session = cipher.decrypt(encrypted_session)
            chat_id = await asyncio.wait_for(
                _find_joined_chat(
                    telegram_session, settings.telegram_api_id, settings.telegram_api_hash
                ), timeout=50
            )
        except Exception as exc:
            print(
                f"WOLVES_SETUP reader_unavailable account={account_id} error={type(exc).__name__}",
                flush=True,
            )
            continue
        if chat_id is None:
            continue

        with factory() as session:
            existing = session.scalar(
                select(Source)
                .where(Source.chat_id == chat_id, Source.status != "revoked")
                .order_by(Source.created_at.asc())
            )
            # One-time activation: if the provider has since been manually
            # paused, never reactivate it during a later application restart.
            already_enrolled = existing is not None
            if already_enrolled:
                print(
                    f"WOLVES_SETUP already_registered source={existing.id} status={existing.status}",
                    flush=True,
                )
                return

            actor = {
                "id": owner_id,
                "role": "owner",
                "display_name": "Wolves Trading requested onboarding",
            }
            try:
                selected = await service.select_source(
                    session, actor=actor, account_id=account_id, chat_id=chat_id
                )
                source = session.get(Source, selected.source_id)
                if source is None:
                    raise RuntimeError("Selected source was not persisted")
                source.declared_instrument = "XAUUSD"
                session.commit()
                service.change_source_status(
                    session, actor=actor, source_id=source.id, new_status="live"
                )
            except Exception as exc:
                session.rollback()
                print(
                    f"WOLVES_SETUP enrollment_failed account={account_id} error={type(exc).__name__}",
                    flush=True,
                )
                return

            print(
                f"WOLVES_SETUP registered source={source.id} chat={chat_id} status=live",
                flush=True,
            )
            return

    print(
        "WOLVES_SETUP not_joined_in_connected_reader; no source or trade state changed",
        flush=True,
    )


if __name__ == "__main__":
    try:
        asyncio.run(onboard())
    except Exception as exc:
        # Source discovery must never take down the existing trading service.
        print(f"WOLVES_SETUP setup_unavailable error={type(exc).__name__}", flush=True)
