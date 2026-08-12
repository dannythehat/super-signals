"""One-shot operational helper to remove a known duplicate Telegram source safely.

Uses the existing source-service unselect semantics so historical evidence remains in
PostgreSQL while the final reader link is removed and the source becomes revoked.
This module is temporary Day 28 acceptance/cleanup plumbing.
"""

from __future__ import annotations

import os
from uuid import UUID

from app.db import get_session_factory
from app.telegram_source_service import get_telegram_source_service


def run() -> None:
    source_id = UUID(os.environ["SUPER_SIGNALS_REMOVE_SOURCE_ID"])
    account_id = UUID(os.environ["SUPER_SIGNALS_REMOVE_SOURCE_ACCOUNT_ID"])
    actor_id = UUID(os.environ["SUPER_SIGNALS_REMOVE_SOURCE_ACTOR_ID"])

    service = get_telegram_source_service()
    session_factory = get_session_factory()
    with session_factory() as session:
        service.unselect_source(
            session,
            actor={"id": actor_id, "display_name": "Trading Admin", "role": "trading_admin"},
            account_id=account_id,
            source_id=source_id,
        )


if __name__ == "__main__":
    run()
