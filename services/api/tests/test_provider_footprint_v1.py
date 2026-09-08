from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.provider_aware_ai_pipeline import ProviderAwareProductionAiPipeline
from app.provider_footprint_v1 import FOOTPRINT_VERSION, ProviderFootprintService


def test_provider_footprint_learns_edits_deletes_replies_and_management_without_ai_price_leak() -> None:
    now = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
    messages = [
        {
            "posted_at": now,
            "deleted_at": None,
            "raw_text": "BUY GOLD NOW 4500 TP 4510 SL 4490 secure runner",
            "raw_payload": {"reply_to_message_id": None},
            "revision_count": 2,
            "first_edit_at": now + timedelta(minutes=2),
        },
        {
            "posted_at": now - timedelta(minutes=20),
            "deleted_at": now - timedelta(minutes=5),
            "raw_text": "Get ready family, setup coming soon",
            "raw_payload": {"reply_to_message_id": 100},
            "revision_count": 0,
            "first_edit_at": None,
        },
    ]
    revisions = [
        {"raw_text": "BUY GOLD 4498 TP 4512 SL 4488 secure here", "edited_at": now + timedelta(minutes=2)},
        {"raw_text": "TP1 done move SL to BE", "edited_at": now + timedelta(minutes=4)},
    ]
    signals = [
        {
            "side": "BUY",
            "order_type": "market",
            "entry_low": Decimal("4498"),
            "entry_high": Decimal("4500"),
            "stop_loss": Decimal("4488"),
            "take_profits": [Decimal("4510"), Decimal("4520")],
            "has_open_runner": True,
            "source_revision_index": 1,
            "source_posted_at": now,
        }
    ]
    events = [
        {
            "event_type": "stop_moved_to_entry",
            "occurred_at": now + timedelta(minutes=8),
            "signal_id": uuid4(),
            "source_posted_at": now,
        }
    ]

    footprint = ProviderFootprintService.build(
        uuid4(), "shadow", messages, revisions, signals, events
    )

    assert footprint["profile_version"] == FOOTPRINT_VERSION
    assert footprint["research_only"] is True
    assert footprint["live_money_execution_allowed"] is False
    assert footprint["message_behaviour"]["edited_messages"] == 1
    assert footprint["message_behaviour"]["deleted_messages"] == 1
    assert footprint["message_behaviour"]["reply_messages"] == 1
    assert footprint["trade_geometry"]["signal_from_revision_rate"] == 1.0
    assert footprint["management_fingerprint"]["management_style"] == "active_management"
    assert footprint["identity_fingerprint"]["cross_provider_match_authority"] is False

    # Research may retain aggregate geometry, but the interpretation packet must not.
    assert footprint["trade_geometry"]["median_stop_distance"] is not None
    interpretation = footprint["interpretation_context"]
    rendered = json.dumps(interpretation, sort_keys=True)
    for historical_price in ("4500", "4498", "4488", "4510", "4520"):
        assert historical_price not in rendered
    assert "median_stop_distance" not in rendered
    assert interpretation["safety_note"].startswith("Behavioural context only")


def test_provider_footprint_is_semantic_stable_without_refresh_clock() -> None:
    source_id = uuid4()
    posted = datetime(2026, 9, 8, 7, 30, tzinfo=UTC)
    messages = [{
        "posted_at": posted,
        "deleted_at": None,
        "raw_text": "Wait for confirmation then secure profits",
        "raw_payload": {},
        "revision_count": 0,
        "first_edit_at": None,
    }]
    first = ProviderFootprintService.build(source_id, "shadow", messages, [], [], [])
    second = ProviderFootprintService.build(source_id, "shadow", messages, [], [], [])

    assert first == second
    assert first["evidence_as_of_utc"] == posted.isoformat()
    assert "generated_at" not in json.dumps(first)


def test_pit_ai_context_exposes_only_sanitised_footprint_subset() -> None:
    snapshot = {
        "profile_metadata": {
            "footprint_v1": {
                "trade_geometry": {"median_stop_distance": 12.5, "median_entry_zone_width": 3.0},
                "interpretation_context": {
                    "message_sequence": "edit_completed_setups",
                    "provider_vocabulary": ["secure", "book", "runner"],
                    "safety_note": "Behavioural context only",
                },
            }
        }
    }

    context = ProviderAwareProductionAiPipeline._footprint_context(snapshot)
    assert context["message_sequence"] == "edit_completed_setups"
    assert "trade_geometry" not in context
    assert "median_stop_distance" not in context


def test_drift_is_observational_and_never_auto_applies() -> None:
    now = datetime(2026, 9, 8, 8, 0, tzinfo=UTC)
    recent = [
        {"side": "BUY", "entry_low": 4500, "entry_high": 4510, "has_open_runner": True, "source_revision_index": 1}
        for _ in range(20)
    ]
    prior = [
        {"side": "SELL", "entry_low": 4500, "entry_high": 4500, "has_open_runner": False, "source_revision_index": 0}
        for _ in range(20)
    ]
    recent_messages = [{"raw_text": "secure runner book partial", "posted_at": now} for _ in range(30)]
    prior_messages = [{"raw_text": "hold target patience", "posted_at": now} for _ in range(30)]

    drift = ProviderFootprintService._drift(recent, prior, recent_messages, prior_messages)
    assert drift["status"] == "candidate_drift"
    assert drift["candidate_reasons"]
    assert drift["auto_apply"] is False
