from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.aidy_historical_toolbox import (
    HISTORICAL_CALENDAR_TOOL_SCHEMA,
    HISTORICAL_EVIDENCE_TOOL_NAME,
    HISTORICAL_EVIDENCE_TOOL_SCHEMA,
    build_historical_tool_executor,
    historical_toolbox_manifest,
)
from app.aidy_reasoning_engine import CALENDAR_TOOL_NAME


def _payload() -> dict:
    return {
        "provider_evidence_claims": [{"id": "provider.performance.overall", "value": 0.6}],
        "market_context": {"session": "new_york", "event_timing": "unknown"},
        "event_liquidity_execution_context": {"risk_state": "normal"},
        "provider_alpha_analogue_context": {"historical_analogue": {"analogues": []}},
        "probability_ev_management_context": {"state": "available"},
        "failure_self_critique_context": {"unknown_gate": {"state": "known"}},
        "recent_messages": ["Buy Gold"],
    }


def test_manifest_routes_connected_and_unconnected_surfaces_without_guessing() -> None:
    manifest = historical_toolbox_manifest(_payload())
    assert manifest["contract_version"] == "aidy_historical_toolbox_manifest_v1"
    assert manifest["target_outcome_available"] is False
    assert manifest["research_only"] is True
    assert {item["name"] for item in manifest["on_demand_tools"]} == {
        "get_recent_candles",
        CALENDAR_TOOL_NAME,
        HISTORICAL_EVIDENCE_TOOL_NAME,
    }
    standing = {item["surface"]: item["status"] for item in manifest["standing_surfaces"]}
    assert standing["provider_history"] == "available"
    assert standing["historical_analogues"] == "available"
    missing = {
        item["surface"]: item["status"]
        for item in manifest["not_connected_research_surfaces"]
    }
    assert missing["rates_macro_vintages"] == "unknown_not_connected_to_historical_contract"
    assert missing["cme_contract_state"] == "unknown_not_connected_to_historical_contract"


def test_historical_calendar_tool_is_callable_and_never_exposes_realized_outcomes() -> None:
    payload = _payload()
    executor = build_historical_tool_executor(
        payload=payload,
        signal_posted_at=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    )
    result = asyncio.run(
        executor(
            CALENDAR_TOOL_NAME,
            {"hours_before": 1, "hours_after": 2, "min_impact": "high"},
        )
    )
    assert result["research_only"] is True
    assert result["target_outcome_available"] is False
    assert result["exact_pit_claimed"] is False
    assert any(event["title"] == "Consumer Price Index" for event in result["events"])
    for event in result["events"]:
        assert event["realized_outcome_available"] is False
        assert event["forecast"] == ""
        assert event["previous"] == ""


def test_historical_evidence_inspector_returns_only_frozen_case_surface() -> None:
    payload = _payload()
    executor = build_historical_tool_executor(
        payload=payload,
        signal_posted_at=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    )
    result = asyncio.run(
        executor(HISTORICAL_EVIDENCE_TOOL_NAME, {"surface": "provider_history"})
    )
    assert result["status"] == "available"
    assert result["target_outcome_available"] is False
    assert result["evidence"] == payload["provider_evidence_claims"]


def test_historical_tool_schemas_are_strict_and_distinct() -> None:
    assert HISTORICAL_CALENDAR_TOOL_SCHEMA["name"] == CALENDAR_TOOL_NAME
    assert HISTORICAL_EVIDENCE_TOOL_SCHEMA["name"] == HISTORICAL_EVIDENCE_TOOL_NAME
    assert HISTORICAL_CALENDAR_TOOL_SCHEMA["parameters"]["additionalProperties"] is False
    assert HISTORICAL_EVIDENCE_TOOL_SCHEMA["parameters"]["additionalProperties"] is False
