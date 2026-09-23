from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.provider_adaptive_profile import AdaptiveProviderProfileService, mask_language_example
from app.provider_management_language_audit import (
    ProviderManagementLanguageAuditService,
    is_management_language_candidate,
    management_phrase_families,
)
from app.shadow_signal_ledger import ShadowAwareCanonicalSignalLedger
from app.shadow_trading_service_v4 import ShadowTradeService


def test_adaptive_profile_learns_zone_and_management_grammar_without_prices() -> None:
    now = datetime.now(timezone.utc)
    decisions = [
        {
            "decision": "new_trade",
            "action": "execute",
            "revision_index": 1,
            "raw_text": "BUY GOLD 4385-4376 TP1 4390 TP2 4400 SL 4371 second entry",
        },
        {
            "decision": "trade_update",
            "action": "apply_update",
            "revision_index": 0,
            "raw_text": "TP1 hit - move SL to BE and leave runner open",
        },
        {
            "decision": "preparation",
            "action": "ignore",
            "revision_index": 0,
            "raw_text": "Get ready for gold",
        },
    ]
    signals = [
        {
            "symbol": "XAUUSD",
            "side": "BUY",
            "order_type": "pending",
            "entry_low": Decimal("4376"),
            "entry_high": Decimal("4385"),
            "has_open_runner": True,
            "source_revision_index": 1,
            "source_posted_at": now,
            "original_text": "BUY GOLD 4385-4376 second entry runner",
        }
    ]

    payload = AdaptiveProviderProfileService._payload(
        uuid4(),
        {"title": "Gold Test Provider", "status": "shadow"},
        decisions,
        signals,
        [],
        [],
    )
    language = payload["language"]
    assert language["instrument_bucket"] == "xauusd_dedicated"
    assert language["entry_bucket"] == "zone"
    assert language["order_bucket"] == "pending"
    assert language["sequence_bucket"] == "edit_completed_setup"
    assert language["management_bucket"] == "active_management"
    assert language["traits"]["uses_layered_entries"] is True
    assert language["traits"]["uses_runner_language"] is True
    assert language["traits"]["uses_breakeven_language"] is True
    example = " ".join(language["grammar_examples_masked"]["new_trade"])
    assert "4385" not in example
    assert "4371" not in example
    assert "<N>" in example


def test_fast_win_slow_loss_pattern_requires_real_evidence_and_never_auto_applies() -> None:
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(12):
        rows.append(
            {
                "side": "BUY",
                "opened_at": now,
                "closed_at": now + timedelta(minutes=5 + index % 4),
                "cash_pnl": Decimal("10"),
            }
        )
    for index in range(8):
        rows.append(
            {
                "side": "BUY",
                "opened_at": now,
                "closed_at": now + timedelta(minutes=25 + index),
                "cash_pnl": Decimal("-10"),
            }
        )

    profile = AdaptiveProviderProfileService._performance(rows, [])
    assert profile["closed_outcomes"] == 20
    assert profile["stale_trade_pattern"]["status"] == "candidate"
    assert profile["stale_trade_pattern"]["watch_after_minutes"] >= 5
    assert profile["stale_trade_pattern"]["auto_apply"] is False


def test_language_mask_removes_urls_and_execution_values() -> None:
    value = mask_language_example(
        "BUY GOLD 4385-4376 TP1 4390 SL 4371 https://example.com/signal"
    )
    assert "4385" not in value
    assert "4371" not in value
    assert "https://" not in value
    assert "<N>" in value
    assert "<URL>" in value


def test_fair_benchmark_accepts_shadow_testing_and_live_sources() -> None:
    source = inspect.getsource(ShadowTradeService.record_signal)
    assert "'shadow','testing','live'" in source


def test_testing_live_benchmark_mirror_is_outside_broker_router() -> None:
    source = inspect.getsource(ShadowAwareCanonicalSignalLedger._mirror_testing_live_signal)
    assert 'not in {"testing", "live"}' in source
    assert "record_signal" in source


def test_management_language_candidate_detects_live_provider_dialects() -> None:
    assert is_management_language_candidate("Make your best entries risk free")
    assert is_management_language_candidate("XAUUSD CLOSE HALF 67+ PIPS MOVE SL TO ENTRY")
    assert is_management_language_candidate("Book maximum and trail entry to maximum profit levels")
    assert not is_management_language_candidate("Good morning team, charts look clean")


def test_management_phrase_families_are_provider_language_not_execution_authority() -> None:
    families = management_phrase_families(
        "Collect partial and set breakeven, then let the rest run risk-free"
    )
    assert "partial" in families
    assert "breakeven" in families


def test_provider_management_audit_counts_covered_and_unmapped_examples() -> None:
    source_id = uuid4()
    rows = [
        {"raw_text": "Make your best entries risk free"},
        {"raw_text": "XAUUSD CLOSE HALF 67+ PIPS PROFIT MOVE SL TO ENTRY"},
        {"raw_text": "Book maximum and trail entry to maximum profit levels"},
        {"raw_text": "Hello team"},
    ]
    payload = ProviderManagementLanguageAuditService._audit_payload(
        source_id,
        {"provider": "Fixture Provider", "status": "shadow"},
        rows,
    )
    assert payload["messages_scanned"] == 4
    assert payload["management_candidates"] == 3
    assert payload["covered_candidates"] == 2
    assert payload["unmapped_candidates"] == 1
    assert payload["execution_authority"] is False
    assert payload["unmapped_examples_masked"]
    assert all("67" not in value for value in payload["covered_examples_masked"])



def test_provider_playbook_learns_trade_and_update_style_without_prices() -> None:
    source_id = uuid4()
    messages = [
        {
            "raw_text": "TP1 hit move SL to BE",
            "raw_payload": {"reply_to_message_id": 100},
            "edited_at": None,
        },
        {
            "raw_text": "Book partial and hold risk free",
            "raw_payload": {},
            "edited_at": datetime.now(timezone.utc),
        },
        {
            "raw_text": "Good morning team",
            "raw_payload": {},
            "edited_at": None,
        },
    ]
    audit = ProviderManagementLanguageAuditService._audit_payload(
        source_id,
        {
            "provider": "Fixture Provider",
            "status": "testing",
            "style": "scalper",
            "cadence": "scalper",
            "management_bucket": "active_management",
        },
        messages,
    )
    signals = [
        {
            "order_type": "limit",
            "side": "BUY",
            "stop_loss": Decimal("4300"),
            "take_profits": [Decimal("4310"), Decimal("4320"), Decimal("4330")],
            "risk_multiplier": Decimal("2"),
            "has_open_runner": False,
            "entry_low": Decimal("4305"),
            "entry_high": Decimal("4307"),
        },
        {
            "order_type": "limit",
            "side": "BUY",
            "stop_loss": Decimal("4298"),
            "take_profits": [Decimal("4310"), Decimal("4320"), Decimal("4330")],
            "risk_multiplier": Decimal("1"),
            "has_open_runner": True,
            "entry_low": Decimal("4304"),
            "entry_high": Decimal("4306"),
        },
    ]

    playbook = ProviderManagementLanguageAuditService._playbook_payload(
        source_id,
        {
            "provider": "Fixture Provider",
            "status": "testing",
            "style": "scalper",
            "cadence": "scalper",
            "management_bucket": "active_management",
        },
        messages,
        signals,
        audit,
    )

    assert playbook["preferred_order_type"] == "limit"
    assert playbook["usual_tp_count"] == 3
    assert playbook["explicit_stop_loss_pct"] == 100.0
    assert playbook["risk_multiplier_above_one_pct"] == 50.0
    assert playbook["entry_zone_pct"] == 100.0
    assert playbook["reply_linked_management_pct"] == 50.0
    assert playbook["edited_message_pct"] == round(100 / 3, 2)
    rendered = str(playbook)
    for historical_price in ("4300", "4305", "4330"):
        assert historical_price not in rendered
