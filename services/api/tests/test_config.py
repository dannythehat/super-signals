"""Fail-closed environment configuration checks."""

from __future__ import annotations

import pytest

from app.config import get_settings

_REQUIRED_ENVIRONMENT_VARIABLES = (
    "DATABASE_URL",
    "SUPER_SIGNALS_CORS_ORIGINS",
    "SUPER_SIGNALS_FINGERPRINT_SECRET",
)


def _clear_settings() -> None:
    get_settings.cache_clear()


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
    _clear_settings()


@pytest.mark.parametrize("missing_variable", _REQUIRED_ENVIRONMENT_VARIABLES)
def test_production_rejects_missing_required_configuration(
    monkeypatch: pytest.MonkeyPatch,
    missing_variable: str,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example.invalid/super_signals")
    monkeypatch.setenv("SUPER_SIGNALS_CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv(
        "SUPER_SIGNALS_FINGERPRINT_SECRET",
        "unique-production-fingerprint-secret-value",
    )
    monkeypatch.delenv(missing_variable, raising=False)
    _clear_settings()

    with pytest.raises(RuntimeError, match=missing_variable):
        get_settings()
    _clear_settings()


def test_production_rejects_known_or_short_fingerprint_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example.invalid/super_signals")
    monkeypatch.setenv("SUPER_SIGNALS_CORS_ORIGINS", "https://app.example.com")

    for secret in ("development-only-change-me", "too-short"):
        monkeypatch.setenv("SUPER_SIGNALS_FINGERPRINT_SECRET", secret)
        _clear_settings()
        with pytest.raises(RuntimeError, match="at least 32 characters"):
            get_settings()
    _clear_settings()


def test_production_accepts_complete_explicit_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUPER_SIGNALS_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example.invalid/super_signals")
    monkeypatch.setenv("SUPER_SIGNALS_CORS_ORIGINS", "https://app.example.com")
    monkeypatch.setenv(
        "SUPER_SIGNALS_FINGERPRINT_SECRET",
        "unique-production-fingerprint-secret-value",
    )
    _clear_settings()

    settings = get_settings()

    assert settings.environment == "production"
    assert settings.session_cookie_secure is True
    assert settings.cors_origins == ("https://app.example.com",)
    _clear_settings()
