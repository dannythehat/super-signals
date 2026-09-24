"""Provider coverage summary: understood-but-not-executed observations, per source.

`provider_trade_observations` already records every message the AI supervisor
recognised as a trade signal, update or close, whether or not it was executed (see
`ai_message_pipeline.py::_store_observation`). This is the first endpoint that reads it
back, so a provider whose management language is never acted on shows up in a report
instead of staying invisible until a stuck position surfaces it weeks later.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.main import create_app
from app.security import hash_password
from app.seed import seed_owner
from app.telegram_crypto import TelegramSessionCipher

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


def _login(client: TestClient, email: str, password: str) -> None:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200


def _observation(
    session: Session,
    *,
    message_id,
    source_id,
    decision: str,
    executable: bool,
    reason: str,
    observed_at: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO provider_trade_observations (
                id, message_id, source_id, revision_index, observed_at,
                decision, action, executable, outcome_reason, take_profits,
                decision_source, raw_text_sha256
            ) VALUES (
                :id, :message_id, :source_id, 0, :observed_at,
                :decision, :action, :executable, :reason, '[]'::jsonb,
                'ai_supervisor', :sha
            )
            """
        ),
        {
            "id": uuid4(),
            "message_id": message_id,
            "source_id": source_id,
            "observed_at": observed_at,
            "decision": decision,
            "action": "execute" if executable else "skip",
            "executable": executable,
            "reason": reason,
            "sha": uuid4().hex,
        },
    )


@pytest.fixture()
def coverage_client(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    monkeypatch.setenv("SUPER_SIGNALS_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", "test-fingerprint-secret")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS", TEST_FERNET_KEY)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(DATABASE_URL, future=True)
    cipher = TelegramSessionCipher((TEST_FERNET_KEY,))
    now = datetime.now(UTC)

    with Session(engine) as session:
        owner = seed_owner(session, "owner@example.com", "Danny")
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("owner password 123"), "id": owner.id},
        )
        reader = session.execute(
            text(
                """
                INSERT INTO telegram_accounts (
                    id, owner_user_id, label, phone_number_e164,
                    session_ciphertext, session_fingerprint, status
                ) VALUES (
                    :id, :owner_id, 'Owner reader', '+359881234567',
                    :ciphertext, :fingerprint, 'connected'
                ) RETURNING id
                """
            ),
            {
                "id": uuid4(),
                "owner_id": owner.id,
                "ciphertext": cipher.encrypt("coverage-reader"),
                "fingerprint": cipher.fingerprint("coverage-reader"),
            },
        ).scalar_one()

        def make_source(chat_id: int, title: str, status: str = "testing"):
            return session.execute(
                text(
                    """
                    INSERT INTO sources (
                        id, telegram_account_id, chat_id, chat_title, source_alias,
                        status, created_by_user_id
                    ) VALUES (
                        :id, :reader_id, :chat_id, :title, :title, :status, :owner_id
                    ) RETURNING id
                    """
                ),
                {
                    "id": uuid4(),
                    "reader_id": reader,
                    "chat_id": chat_id,
                    "title": title,
                    "status": status,
                    "owner_id": owner.id,
                },
            ).scalar_one()

        quiet_provider = make_source(-100301, "Quiet Provider")
        revoked_provider = make_source(-100302, "Revoked Provider", status="revoked")
        untouched_provider = make_source(-100303, "Untouched Provider")

        message_counter = iter(range(1, 1000))

        def make_message(source_id):
            return session.execute(
                text(
                    """
                    INSERT INTO messages (
                        id, source_id, telegram_message_id, raw_text, posted_at,
                        ingestion_status, content_sha256
                    )
                    VALUES (
                        :id, :source_id, :tg_id, 'irrelevant for this report', now(),
                        'received', :sha
                    )
                    RETURNING id
                    """
                ),
                {
                    "id": uuid4(),
                    "source_id": source_id,
                    "tg_id": next(message_counter),
                    "sha": uuid4().hex,
                },
            ).scalar_one()

        # Quiet Provider: two understood-but-declined updates (both unsupported_management),
        # one earlier declined new_trade for a different reason, and one executed trade -
        # the executed one must not appear in top_reasons or inflate the decline count.
        _observation(
            session,
            message_id=make_message(quiet_provider),
            source_id=quiet_provider,
            decision="trade_update",
            executable=False,
            reason="unsupported_management",
            observed_at=now - timedelta(hours=2),
        )
        _observation(
            session,
            message_id=make_message(quiet_provider),
            source_id=quiet_provider,
            decision="trade_update",
            executable=False,
            reason="unsupported_management",
            observed_at=now - timedelta(hours=1),
        )
        _observation(
            session,
            message_id=make_message(quiet_provider),
            source_id=quiet_provider,
            decision="new_trade",
            executable=False,
            reason="missing_sl",
            observed_at=now - timedelta(hours=3),
        )
        _observation(
            session,
            message_id=make_message(quiet_provider),
            source_id=quiet_provider,
            decision="new_trade",
            executable=True,
            reason="new_trade_confirmed",
            observed_at=now - timedelta(minutes=30),
        )

        # Revoked Provider: has observations, but must never appear in the report.
        _observation(
            session,
            message_id=make_message(revoked_provider),
            source_id=revoked_provider,
            decision="trade_update",
            executable=False,
            reason="unsupported_management",
            observed_at=now - timedelta(hours=1),
        )

        # An observation outside the window must not be counted.
        _observation(
            session,
            message_id=make_message(quiet_provider),
            source_id=quiet_provider,
            decision="trade_update",
            executable=False,
            reason="unsupported_management",
            observed_at=now - timedelta(days=30),
        )

        session.commit()
        quiet_id = quiet_provider
        untouched_id = untouched_provider

    application = create_app()
    with TestClient(application) as client:
        yield client, quiet_id, untouched_id

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def test_provider_coverage_summary_counts_and_excludes_correctly(coverage_client) -> None:
    client, quiet_id, untouched_id = coverage_client
    _login(client, "owner@example.com", "owner password 123")

    response = client.get("/admin/telegram/provider-coverage/summary?days=7")
    assert response.status_code == 200
    rows = {row["source_id"]: row for row in response.json()}

    # A provider with zero observations in the window never appears - nothing to report.
    assert str(untouched_id) not in rows

    # A revoked source never appears, even with real observations in the window.
    assert all(row["source_status"] != "revoked" for row in rows.values())

    quiet = rows[str(quiet_id)]
    assert quiet["source_title"] == "Quiet Provider"
    # 3 in-window observations: 2 unsupported_management + 1 missing_sl. The executed
    # trade and the 30-day-old row are excluded from both the total and the breakdown.
    assert quiet["total_observations"] == 4
    assert quiet["executed_count"] == 1
    assert quiet["understood_not_executed_count"] == 3
    reasons = {item["reason"]: item["count"] for item in quiet["top_reasons"]}
    assert reasons == {"unsupported_management": 2, "missing_sl": 1}


def test_provider_coverage_requires_sources_manage_permission(coverage_client) -> None:
    client, _quiet_id, _untouched_id = coverage_client
    response = client.get("/admin/telegram/provider-coverage/summary")
    assert response.status_code in (401, 403)
