from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

# AIDY is allowed to read live facts and write research-only evidence. It is never
# allowed to gain broker execution, provider-control, or Telegram publication authority.
_FORBIDDEN_IMPORTS = (
    "app.metaapi_trade_gateway",
    "app.metaapi_gateway",
    "app.mt5_connection_manager",
    "app.mt5_connection_service",
    "app.broker_settlement",
    "app.telegram_publisher",
    "app.publisher_config",
    "app.routes.day26_execution",
    "app.routes.day27_management",
)

_FORBIDDEN_ACTIONS = (
    "close_position(",
    "place_order(",
    "_bot_api_call(",
    '"sendMessage"',
    '"pinChatMessage"',
    "UPDATE sources",
    "UPDATE positions",
    "UPDATE mt5_accounts",
    "UPDATE signals",
    "DELETE FROM sources",
    "DELETE FROM positions",
    "DELETE FROM mt5_accounts",
    "DELETE FROM signals",
    "INSERT INTO positions",
    "INSERT INTO telegram_publications",
    "INSERT INTO broker_deals",
)


def _research_sources() -> list[Path]:
    sources = sorted(APP.glob("aidy*.py"))
    sources.extend(
        [
            APP / "provider_trade_scoring_runtime.py",
            APP / "provider_fingerprint_runtime.py",
            APP / "provider_management_language_audit.py",
            APP / "provider_management_language_audit_runtime.py",
        ]
    )
    return sources


def test_aidy_research_lane_has_no_live_execution_or_telegram_authority() -> None:
    violations: list[str] = []
    for path in _research_sources():
        source = path.read_text(encoding="utf-8")
        lowered = source.lower()
        for forbidden in _FORBIDDEN_IMPORTS:
            if forbidden.lower() in lowered:
                violations.append(f"{path.name}: imports {forbidden}")
        for forbidden in _FORBIDDEN_ACTIONS:
            if forbidden.lower() in lowered:
                violations.append(f"{path.name}: contains {forbidden}")
    assert not violations, "\n".join(violations)


def test_aidy_research_lane_is_started_after_live_trading_and_telegram() -> None:
    source = (APP / "main.py").read_text(encoding="utf-8")
    live_ready = "await publisher.start()"
    research_start = 'name="super-signals-research-startup"'
    assert live_ready in source
    assert research_start in source
    assert source.index(live_ready) < source.index(research_start)
    assert "runtime = runtime_type(research_session_factory)" in source
    assert "runtime_type(session_factory)" not in source


def test_aidy_startup_failure_is_fail_open_for_live_lane() -> None:
    source = (APP / "main.py").read_text(encoding="utf-8")
    assert "await asyncio.wait_for(runtime.start(), timeout=5)" in source
    assert "live trading and Telegram remain active" in source
    assert "research_start_task = asyncio.create_task(" in source


def test_telegram_admin_surface_cannot_fall_back_to_legacy_publisher() -> None:
    route = (APP / "routes" / "telegram_publisher.py").read_text(encoding="utf-8")
    assert "CanonicalTelegramPublisherManager" in route
    assert "telegram_publisher_policy" not in route
    assert "Day19TelegramPublisherManager" not in route


def test_main_constructs_only_canonical_telegram_publisher() -> None:
    source = (APP / "main.py").read_text(encoding="utf-8")
    assert "publisher = CanonicalTelegramPublisherManager(" in source
    for legacy in (
        "Day19TelegramPublisherManager(",
        "Day20TelegramPublisherManager(",
        "Day34TelegramPublisherManager(",
        "Day34CutoverTelegramPublisherManager(",
    ):
        assert legacy not in source


def test_obsolete_aidy_monkeypatch_watchdog_is_gone() -> None:
    assert not (APP / "aidy_runtime_watchdog_hotfix.py").exists()
    source = (APP / "aidy_shadow_runtime.py").read_text(encoding="utf-8")
    assert "super-signals-aidy-runtime-supervisor" in source
    assert "aidy_context_consecutive_failures" in source
