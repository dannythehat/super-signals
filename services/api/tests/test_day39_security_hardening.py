"""Focused Day 39 security-hardening proofs without touching live user data."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import app.auth_service as auth_service
from app.auth_rate_limit_day39 import (
    ADMIN_SETUP_POLICY,
    LOGIN_POLICY,
    RECOVERY_POLICY,
    subject_hash,
)
from app.security import privacy_hash

USER_ID = UUID("11111111-1111-4111-8111-111111111111")


class _Result:
    def __init__(self, row=None):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class _Session:
    def __init__(self, active_session):
        self.active_session = active_session
        self.updated = False
        self.commits = 0

    def execute(self, statement, params=None):  # noqa: ANN001
        sql = str(statement)
        if "SELECT s.id AS session_id" in sql:
            return _Result(self.active_session)
        if "UPDATE auth_sessions" in sql:
            self.updated = True
            return _Result()
        raise AssertionError(f"unexpected SQL in Day 39 session test: {sql[:80]}")

    def commit(self):
        self.commits += 1


def _identity():
    return {
        "id": USER_ID,
        "email": "member@example.com",
        "display_name": "Member",
        "two_factor_enabled": False,
        "passkey_enabled": False,
        "roles": ["user"],
        "role": "user",
        "permissions": ["dashboard.view"],
    }


def test_session_cookie_is_rejected_when_browser_fingerprint_changes(monkeypatch) -> None:
    secret = "day39-test-fingerprint-secret-which-is-long-enough"
    stored = privacy_hash("Browser A", secret)
    session = _Session(
        {
            "session_id": UUID("22222222-2222-4222-8222-222222222222"),
            "user_id": USER_ID,
            "expires_at": None,
            "user_agent_hash": stored,
        }
    )
    monkeypatch.setattr(auth_service, "_load_identity", lambda *_: _identity())
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(session_ttl_seconds=3600),
    )

    identity = auth_service.get_user_for_session(
        session,
        "stolen-cookie-value",
        user_agent="Browser B",
        fingerprint_secret=secret,
    )

    assert identity is None
    assert session.updated is False
    assert session.commits == 0


def test_same_browser_fingerprint_can_renew_session(monkeypatch) -> None:
    secret = "day39-test-fingerprint-secret-which-is-long-enough"
    stored = privacy_hash("Browser A", secret)
    session = _Session(
        {
            "session_id": UUID("22222222-2222-4222-8222-222222222222"),
            "user_id": USER_ID,
            "expires_at": None,
            "user_agent_hash": stored,
        }
    )
    monkeypatch.setattr(auth_service, "_load_identity", lambda *_: _identity())
    monkeypatch.setattr(
        auth_service,
        "get_settings",
        lambda: SimpleNamespace(session_ttl_seconds=3600),
    )

    identity = auth_service.get_user_for_session(
        session,
        "real-cookie-value",
        user_agent="Browser A",
        fingerprint_secret=secret,
    )

    assert identity is not None
    assert identity["id"] == USER_ID
    assert session.updated is True
    assert session.commits == 1


def test_auth_rate_limit_subjects_are_privacy_hashed() -> None:
    secret = "day39-rate-limit-secret-long-enough-for-hmac"
    hashed = subject_hash("  USER@Example.com ", secret)

    assert hashed == subject_hash("user@example.com", secret)
    assert hashed != "user@example.com"
    assert len(hashed) == 64
    assert LOGIN_POLICY.max_attempts == 5
    assert LOGIN_POLICY.block_seconds == 15 * 60
    assert RECOVERY_POLICY.max_attempts == 3
    assert RECOVERY_POLICY.block_seconds == 60 * 60
    assert ADMIN_SETUP_POLICY.max_attempts == 5


def test_audit_table_is_database_append_only_by_migration_definition() -> None:
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0001_core_data_model.py"
    ).read_text(encoding="utf-8")

    assert "CREATE TRIGGER audit_events_no_update" in migration
    assert "BEFORE UPDATE ON audit_events" in migration
    assert "CREATE TRIGGER audit_events_no_delete" in migration
    assert "BEFORE DELETE ON audit_events" in migration
    assert "audit_events is append-only" in migration


def test_day39_rate_limit_migration_never_stores_raw_credentials() -> None:
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0022_day39_auth_rate_limits.py"
    ).read_text(encoding="utf-8")

    assert '"subject_hash"' in migration
    assert '"email"' not in migration
    assert '"password"' not in migration
    assert '"ip_address"' not in migration
    assert '"token"' not in migration
