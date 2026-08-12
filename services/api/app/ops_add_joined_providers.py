"""One-shot operational helper for adding Danny's newly joined paper providers.

This module is intentionally conservative: it discovers the connected Telegram dialogs,
requires exactly one title match for each approved provider, selects those exact chat IDs
through TelegramSourceService, sets them to testing, and reactivates the existing
FXTradingVision source to testing. It never enables live trading.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select

from app.db import get_session_factory
from app.models import Source
from app.telegram_source_service import get_telegram_source_service

OWNER_USER_ID = UUID("ea604df2-f8ee-47d1-bc51-f0078dbf160d")
TELEGRAM_ACCOUNT_ID = UUID("f0c57dc1-7679-42f8-b90a-a3dde13a525c")
FX_SOURCE_ID = UUID("1f1f1310-fa03-4fb9-9044-636a1d4a8c21")

# Match titles, not Telegram search results. The user has already joined the official
# channels verified from the providers' own websites.
TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SureShot GOLD", ("sureshot", "gold")),
    ("United Kings", ("united", "kings")),
    ("PipXpert", ("pipxpert",)),
)


def _matches(title: str, tokens: tuple[str, ...]) -> bool:
    normalized = " ".join(title.casefold().split())
    return all(token in normalized for token in tokens)


async def run() -> None:
    session_factory = get_session_factory()
    service = get_telegram_source_service()
    actor = {
        "id": OWNER_USER_ID,
        "role": "owner",
        "display_name": "Danny",
        "email": "dannythehat2@gmail.com",
    }

    with session_factory() as session:
        dialogs = await service.discover_sources(
            session,
            actor=actor,
            account_id=TELEGRAM_ACCOUNT_ID,
        )

    resolved: list[tuple[str, int, str]] = []
    for label, tokens in TARGETS:
        candidates = [item for item in dialogs if _matches(item.title, tokens)]
        if len(candidates) != 1:
            candidate_text = ", ".join(f"{item.title} ({item.chat_id})" for item in candidates)
            raise RuntimeError(
                f"ops_provider_match_not_unique provider={label!r} count={len(candidates)} "
                f"candidates=[{candidate_text}]"
            )
        item = candidates[0]
        resolved.append((label, item.chat_id, item.title))

    print("Ops provider discovery resolved:")
    for label, chat_id, title in resolved:
        print(f"  {label}: title={title!r} chat_id={chat_id}")

    selected_results = []
    for label, chat_id, _title in resolved:
        with session_factory() as session:
            item = await service.select_source(
                session,
                actor=actor,
                account_id=TELEGRAM_ACCOUNT_ID,
                chat_id=chat_id,
            )
            if item.source_id is None:
                raise RuntimeError(f"ops_source_id_missing provider={label!r}")
            status_item = service.change_source_status(
                session,
                actor=actor,
                source_id=item.source_id,
                new_status="testing",
            )
            selected_results.append(
                (label, str(item.source_id), item.title, status_item.status)
            )

    with session_factory() as session:
        fx = session.scalar(select(Source).where(Source.id == FX_SOURCE_ID))
        if fx is None:
            raise RuntimeError("ops_fxtradingvision_source_missing")
        fx_change = service.change_source_status(
            session,
            actor=actor,
            source_id=FX_SOURCE_ID,
            new_status="testing",
        )

    print("Ops provider selection PASSED:")
    for label, source_id, title, status in selected_results:
        print(f"  {label}: source_id={source_id} title={title!r} status={status}")
    print(
        "  FXTradingVision: "
        f"source_id={FX_SOURCE_ID} title={fx_change.title!r} "
        f"previous_status={fx_change.previous_status} status={fx_change.status}"
    )
    print("  live_trading_enabled=False (Day 28 allow-list unchanged)")


if __name__ == "__main__":
    asyncio.run(run())
