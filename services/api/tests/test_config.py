"""Fail-closed environment configuration checks."""

from __future__ import annotations

import pytest

from app.config import get_settings

TEST_TELEGRAM_KEY = "ubLguclBbK8FnkHkIye7Wv93Iplvn2UepTnT7bHBez8="
_REQUIRED_ENVIRONMENT_VARIABLES = (
    "DATABASE_URL",
    "SUPER_SIGNALS_CORS_ORIGINS",
    "SUPER_SIGNALS_FINGERPRINT_SECRET",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "SUPER_SIGNALS_TELEGRAM_SESSION_KEYS",
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


def test_development_uses_non_secret_local_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_ENV", "development")
    for variable in _REQUIRED_ENVIRONMENT_VARIABLES:
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
