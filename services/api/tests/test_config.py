"""Fail-closed environment configuration checks."""

from __future__ import annotations

import base64
import hashlib

import pytest

from app.config import get_settings

TEST_TELEGRAM_KEY = "ubLguclBbK8FnkHkIye7Wv93Iplvn2UepTnT7bHBez8="
TEST_TELEGRAM_SECRET = "render-generated-session-secret-value-1234567890"
_REQUIRED_ENVIRONMENT_VARIABLES = (
    "DATABASE_URL",
    "SUPER_SIGNALS_CORS_ORIGINS",
    "SUPER_SIGNALS_FINGERPRINT_SECRET",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
)
_SESSION_ENVIRONMENT_VARIABLES = (
    "SUPER_SIGNALS_TELEGRAM_SESSION_KEYS",
    "SUPER_SIGNALS_TELEGRAM_SESSION_SECRET",
)


def _clear_settings() -> None:
    get_settings.cache_clear()


def _set_complete_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example.invalid/super_signals")
    monkeypatch.setenv("SUPER_SIGNALS_CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv(
        "SUPER_SIGNALS_FINGERPRINT_SECRET",
        "unique-production-fingerprint-secret-value",
    )
    monkeypatch.setenv("TELEGRAM_API_ID", "123456")
    monkeypatch.setenv("TELEGRAM_API_HASH", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS", TEST_TELEGRAM_KEY)
    monkeypatch.delenv("SUPER_SIGNALS_TELEGRAM_SESSION_SECRET", raising=False)


def test_development_uses_non_secret_local_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_ENV", "development")
    for variable in (*_REQUIRED_ENVIRONMENT_VARIABLES, *_SESSION_ENVIRONMENT_VARIABLES):
        monkeypatch.delenv(variable, raising=False)
    _clear_settings()

    settings = get_settings()

    assert settings.environment == "development"
    assert settings.session_cookie_secure is False
    assert settings.session_fingerprint_secret == "development-only-change-me"
    assert settings.telegram_api_id is None
    assert settings.telegram_api_hash is None
    assert len(settings.telegram_session_keys) == 1
    _clear_settings()


@pytest.mark.parametrize("missing_variable", _REQUIRED_ENVIRONMENT_VARIABLES)
def test_production_rejects_missing_required_configuration(
    monkeypatch: pytest.MonkeyPatch,
    missing_variable: str,
) -> None:
    _set_complete_production_environment(monkeypatch)
    monkeypatch.delenv(missing_variable, raising=False)
    _clear_settings()

    with pytest.raises(RuntimeError, match=missing_variable):
        get_settings()
    _clear_settings()


def test_production_requires_one_session_encryption_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)
    for variable in _SESSION_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    _clear_settings()

    with pytest.raises(RuntimeError, match="TELEGRAM_SESSION_KEYS or"):
        get_settings()
    _clear_settings()


def test_production_rejects_known_or_short_fingerprint_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)

    for secret in ("development-only-change-me", "too-short"):
        monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", secret)
        _clear_settings()
        with pytest.raises(RuntimeError, match="at least 32 characters"):
            get_settings()
    _clear_settings()


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    (
        ("TELEGRAM_API_ID", "not-a-number", "positive integer"),
        ("TELEGRAM_API_ID", "0", "positive integer"),
        ("TELEGRAM_API_HASH", "not-a-valid-hash", "32-character hexadecimal"),
        (
            "SUPER_SIGNALS_TELEGRAM_SESSION_KEYS",
            "not-a-fernet-key",
            "invalid Fernet key",
        ),
        ("SUPER_SIGNALS_TELEGRAM_QR_TTL_SECONDS", "0", "positive integer"),
    ),
)
def test_production_rejects_invalid_telegram_configuration(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    message: str,
) -> None:
    _set_complete_production_environment(monkeypatch)
    monkeypatch.setenv(variable, value)
    _clear_settings()

    with pytest.raises(RuntimeError, match=message):
        get_settings()
    _clear_settings()


def test_production_rejects_short_generated_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)
    monkeypatch.delenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_SECRET", "too-short")
    _clear_settings()

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        get_settings()
    _clear_settings()


def test_production_rejects_ambiguous_session_encryption_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_SECRET", TEST_TELEGRAM_SECRET)
    _clear_settings()

    with pytest.raises(RuntimeError, match="Configure only one"):
        get_settings()
    _clear_settings()


def test_render_generated_session_secret_derives_a_valid_fernet_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)
    monkeypatch.delenv("SUPER_SIGNALS_TELEGRAM_SESSION_KEYS")
    monkeypatch.setenv("SUPER_SIGNALS_TELEGRAM_SESSION_SECRET", TEST_TELEGRAM_SECRET)
    _clear_settings()

    settings = get_settings()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(TEST_TELEGRAM_SECRET.encode("utf-8")).digest()
    ).decode("ascii")

    assert settings.telegram_session_keys == (expected,)
    _clear_settings()


def test_render_database_url_uses_the_installed_psycopg_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@db.example.com/app")
    _clear_settings()

    settings = get_settings()

    assert settings.database_url == "postgresql+psycopg://user:password@db.example.com/app"
    _clear_settings()


def test_production_accepts_complete_explicit_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)
    _clear_settings()

    settings = get_settings()

    assert settings.environment == "production"
    assert settings.session_cookie_secure is True
    assert settings.cors_origins == ("https://app.example.com",)
    assert settings.telegram_api_id == 123456
    assert settings.telegram_api_hash == "0123456789abcdef0123456789abcdef"
    assert settings.telegram_session_keys == (TEST_TELEGRAM_KEY,)
    _clear_settings()


def test_render_open_alias_supplies_ai_supervisor_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("Open", "render-open-key")
    _clear_settings()

    settings = get_settings()

    assert settings.ai_supervisor_api_key == "render-open-key"
    assert settings.ai_supervisor_timeout_seconds == 12
    _clear_settings()


def test_standard_openai_key_takes_precedence_over_render_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_complete_production_environment(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "canonical-key")
    monkeypatch.setenv("Open", "legacy-render-key")
    _clear_settings()

    settings = get_settings()

    assert settings.ai_supervisor_api_key == "canonical-key"
    _clear_settings()
