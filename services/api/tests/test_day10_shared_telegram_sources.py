"""Day 10 multi-admin Telegram ownership and shared-source acceptance checks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, get_session_factory
from app.main import create_app
from app.models import Message, Source, TelegramAccount
from app.routes.telegram_accounts import provide_telegram_connection_service
from app.routes.telegram_sources import provide_telegram_source_service
from app.security import hash_password
from app.seed import seed_account, seed_owner
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_source_gateway import TelegramSelectableDialog
from app.telegram_source_service import TelegramSourceService, get_telegram_source_service
from app.telegram_service import TelegramConnectionService, get_telegram_connection_service

DATABASE_URL = os.getenv("DATABASE_URL")
API_ROOT = Path(__file__).resolve().parents[1]
TEST_FERNET_KEY = "SY6ZSyA-C-HLcoOd_Wy60cnF3wVElxxhhDxwPVkrQtA="


def _alembic_config() -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    if DATABASE_URL:
        config.set_main_option("sqlalchemy.url", DATABASE_URL)
    return config


@dataclass
class MultiReaderSourceGateway:
    dialogs_by_session: dict[str, list[TelegramSelectableDialog]] = field(default_factory=dict)
    listed_sessions: list[str] = field(default_factory=list)

    async def list_selectable_dialogs(self, session_string: str):
        self.listed_sessions.append(session_string)
        return list(self.dialogs_by_session.get(session_string, []))


def _login(client: TestClient, email: str, password: str) -> None:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200


@pytest.fixture()
def day10_client(monkeypatch: pytest.MonkeyPatch):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL is required for PostgreSQL integration tests")

    monkeypatch.setenv("SUPER_SIGNALS_ENV", "test")
    monkeypatch.setenv("SUPER_SIGNALS_COOKIE_SECURE", "false")
    monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", "test-fingerprint-secret")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS", TEST_FERNET_KEY)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_telegram_source_service.cache_clear()
    get_telegram_connection_service.cache_clear()

    config = _alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(DATABASE_URL, future=True)
    cipher = TelegramSessionCipher((TEST_FERNET_KEY,))

    owner_sessions = [f"owner-reader-{index}" for index in range(1, 5)]
    friend_session = "friend-reader-1"

    with Session(engine) as session:
        owner = seed_owner(session, "owner@example.com", "Danny")
        friend = seed_account(session, "friend@example.com", "Friend", "trading_admin")
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("owner password 123"), "id": owner.id},
        )
        session.execute(
            text("UPDATE users SET password_hash = :hash WHERE id = :id"),
            {"hash": hash_password("friend password 123"), "id": friend.id},
        )

        owner_accounts: list[TelegramAccount] = []
        for index, session_string in enumerate(owner_sessions, start=1):
            account = TelegramAccount(
                owner_user_id=owner.id,
                label=f"Owner reader {index}",
                phone_number_e164=f"+35988123456{index}",
                session_ciphertext=cipher.encrypt(session_string),
                session_fingerprint=cipher.fingerprint(session_string),
                status="connected",
            )
            session.add(account)
            owner_accounts.append(account)

        friend_account = TelegramAccount(
            owner_user_id=friend.id,
            label="Friend reader 1",
            phone_number_e164="+447700900123",
            session_ciphertext=cipher.encrypt(friend_session),
            session_fingerprint=cipher.fingerprint(friend_session),
            status="connected",
        )
        session.add(friend_account)
        session.commit()
        for account in owner_accounts:
            session.refresh(account)
        session.refresh(friend_account)
        owner_account_ids = [account.id for account in owner_accounts]
        friend_account_id = friend_account.id

    source_gateway = MultiReaderSourceGateway(
        dialogs_by_session={
            owner_sessions[0]: [
                TelegramSelectableDialog(
                    chat_id=-100111,
                    title="Owner Gold Signals",
                    kind="channel",
                )
            ],
            friend_session: [
                TelegramSelectableDialog(
                    chat_id=-100111,
                    title="Owner Gold Signals",
                    kind="channel",
                ),
                TelegramSelectableDialog(
                    chat_id=-200222,
                    title="Friend FX Signals",
                    kind="group",
                ),
            ],
        }
    )
    source_service = TelegramSourceService(source_gateway, cipher)
    connection_service = TelegramConnectionService(SimpleNamespace(), cipher)

    application = create_app()
    application.dependency_overrides[provide_telegram_source_service] = lambda: source_service
    application.dependency_overrides[provide_telegram_connection_service] = lambda: connection_service

    with TestClient(application) as client:
        yield client, engine, owner_account_ids, friend_account_id

    command.downgrade(config, "base")
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_telegram_source_service.cache_clear()
    get_telegram_connection_service.cache_clear()


def test_four_private_readers_and_two_admins_share_selected_sources(day10_client) -> None:
    client, engine, owner_account_ids, friend_account_id = day10_client
    owner_primary = owner_account_ids[0]

    _login(client, "owner@example.com", "owner password 123")
    owner_accounts = client.get("/admin/telegram/accounts")
    assert owner_accounts.status_code == 200
    assert len(owner_accounts.json()) == 4
    assert {item["id"] for item in owner_accounts.json()} == {
        str(account_id) for account_id in owner_account_ids
    }
    assert str(friend_account_id) not in owner_accounts.text

    owner_selected = client.post(
        f"/admin/telegram/sources/accounts/{owner_primary}/select",
        json={"chat_id": -100111},
    )
    assert owner_selected.status_code == 201
    assert owner_selected.json()["selected"] is True
    assert owner_selected.json()["status"] == "paused"
    owner_source_id = owner_selected.json()["source_id"]

    owner_shared = client.get("/admin/telegram/sources/shared")
    assert owner_shared.status_code == 200
    owner_shared_rows = owner_shared.json()
    assert len(owner_shared_rows) == 1
    owner_shared_row = owner_shared_rows[0]
    assert {
        key: owner_shared_row[key]
        for key in ("source_id", "chat_id", "title", "status")
    } == {
        "source_id": owner_source_id,
        "chat_id": -100111,
        "title": "Owner Gold Signals",
        "status": "paused",
    }
    assert owner_shared_row["shadow_total"] == 0
    assert owner_shared_row["shadow_open"] == 0
    assert owner_shared_row["shadow_closed"] == 0
    assert owner_shared_row["shadow_wins"] == 0
    assert owner_shared_row["shadow_losses"] == 0
    assert owner_shared_row["shadow_return_percent"] == "0"

    assert client.post("/auth/logout").status_code == 204
    _login(client, "friend@example.com", "friend password 123")

    friend_accounts = client.get("/admin/telegram/accounts")
    assert friend_accounts.status_code == 200
    assert [item["id"] for item in friend_accounts.json()] == [str(friend_account_id)]
    assert all(str(account_id) not in friend_accounts.text for account_id in owner_account_ids)

    # Private reader sessions are not controllable across admins, even by source managers.
    assert client.post(f"/admin/telegram/accounts/{owner_primary}/verify").status_code == 404
    assert client.post(f"/admin/telegram/accounts/{owner_primary}/disconnect").status_code == 404
    assert (
        client.get(f"/admin/telegram/sources/accounts/{owner_primary}/available").status_code
        == 404
    )

    friend_discovery = client.get(
        f"/admin/telegram/sources/accounts/{friend_account_id}/available"
    )
    assert friend_discovery.status_code == 200
    shared_from_owner = next(
        item for item in friend_discovery.json() if item["chat_id"] == -100111
    )
    assert shared_from_owner["selected"] is True
    assert shared_from_owner["source_id"] == owner_source_id
    assert shared_from_owner["managed_by_this_reader"] is False

    # Link the friend's reader to the same logical source: still one Source row,
    # but now there are two valid private readers the future listener can choose from.
    duplicate = client.post(
        f"/admin/telegram/sources/accounts/{friend_account_id}/select",
        json={"chat_id": -100111},
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["source_id"] == owner_source_id
    assert duplicate.json().get("managed_by_this_reader", True) is True

    friend_selected = client.post(
        f"/admin/telegram/sources/accounts/{friend_account_id}/select",
        json={"chat_id": -200222},
    )
    assert friend_selected.status_code == 201
    assert friend_selected.json()["status"] == "paused"

    shared_for_friend = client.get("/admin/telegram/sources/shared")
    assert shared_for_friend.status_code == 200
    assert {item["chat_id"] for item in shared_for_friend.json()} == {-100111, -200222}
    assert all(item["status"] == "paused" for item in shared_for_friend.json())

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Source)) == 2
        reader_link_count = session.scalar(text("SELECT count(*) FROM source_reader_access"))
        assert reader_link_count == 3
        assert session.scalar(select(func.count()).select_from(Message)) == 0
        assert set(session.scalars(select(Source.status)).all()) == {"paused"}

    assert client.post("/auth/logout").status_code == 204
    _login(client, "owner@example.com", "owner password 123")
    shared_for_owner = client.get("/admin/telegram/sources/shared")
    assert shared_for_owner.status_code == 200
    assert {item["chat_id"] for item in shared_for_owner.json()} == {-100111, -200222}
    assert "447700900123" not in shared_for_owner.text
    assert "35988123456" not in shared_for_owner.text

    # Removing one reader does not remove the shared source while the friend still
    # supplies valid access to the same Telegram group.
    removed = client.post(
        f"/admin/telegram/sources/accounts/{owner_primary}/selected/{owner_source_id}/remove"
    )
    assert removed.status_code == 200
    still_shared = client.get("/admin/telegram/sources/shared")
    assert {item["chat_id"] for item in still_shared.json()} == {-100111, -200222}

    with Session(engine) as session:
        owner_source = session.get(Source, owner_source_id)
        assert owner_source is not None
        assert owner_source.status == "paused"
        assert owner_source.telegram_account_id == friend_account_id
        reader_ids = set(
            session.execute(
                text(
                    """
                    SELECT telegram_account_id
                    FROM source_reader_access
                    WHERE source_id = :source_id
                    """
                ),
                {"source_id": owner_source.id},
            ).scalars()
        )
        assert reader_ids == {friend_account_id}
