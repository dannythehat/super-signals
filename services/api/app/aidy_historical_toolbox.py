"""Research-only toolbox registry for AIDY historical stress reasoning.

The stress lab must distinguish between:
- on-demand research tools the model may call;
- standing evidence already attached to the case; and
- AIDY research surfaces that exist elsewhere but are not connected to this historical
  contract.

Nothing in this module grants live execution authority or upgrades reconstructed research
evidence to exact point-in-time evidence.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from app.aidy_historical_research_calendar import historical_schedule_for_day
from app.aidy_reasoning_engine import CALENDAR_TOOL_NAME

HISTORICAL_EVIDENCE_TOOL_NAME = "inspect_historical_evidence"

HISTORICAL_CALENDAR_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": CALENDAR_TOOL_NAME,
    "description": (
        "Fetch the verified retrospective official macro schedule around this historical "
        "Gold signal. It contains scheduled timestamps only: no realized values, surprises, "
        "forecasts or post-release outcomes. Use it when event proximity could materially "
        "change the risk judgement. This is research-only and not exact-PIT evidence."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "hours_before": {"type": "integer", "minimum": 0, "maximum": 72},
            "hours_after": {"type": "integer", "minimum": 0, "maximum": 72},
            "min_impact": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
        },
        "required": ["hours_before", "hours_after", "min_impact"],
    },
}

HISTORICAL_EVIDENCE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": HISTORICAL_EVIDENCE_TOOL_NAME,
    "description": (
        "Inspect one of AIDY's standing historical evidence surfaces in depth. Use this when "
        "a compact prompt field is not enough to judge whether the current trade should be "
        "taken, reduced or rejected. The tool never reveals the target trade outcome."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "surface": {
                "type": "string",
                "enum": [
                    "provider_history",
                    "market_context",
                    "event_liquidity",
                    "historical_analogues",
                    "probability_ev",
                    "self_critique",
                    "recent_messages",
                ],
            }
        },
        "required": ["surface"],
    },
}

_STANDING_SURFACES: tuple[tuple[str, str], ...] = (
    ("provider_history", "provider_evidence_claims"),
    ("market_context", "market_context"),
    ("event_liquidity", "event_liquidity_execution_context"),
    ("historical_analogues", "provider_alpha_analogue_context"),
    ("probability_ev", "probability_ev_management_context"),
    ("self_critique", "failure_self_critique_context"),
    ("recent_messages", "recent_messages"),
)

# These capabilities exist in the wider AIDY research repository but are deliberately not
# represented as available evidence in this reconstructed Super Signals stress contract until
# an auditable historical adapter proves their timestamp/provenance semantics.
_NOT_CONNECTED_RESEARCH_SURFACES: tuple[str, ...] = (
    "rates_macro_vintages",
    "cross_market_asof",
    "cme_contract_state",
    "gvz_implied_volatility",
    "semantic_context_composer",
    "provider_decision_memory",
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("historical_toolbox_timestamp_must_be_timezone_aware")
    return value.astimezone(UTC)


def historical_toolbox_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    standing: list[dict[str, Any]] = []
    for surface, key in _STANDING_SURFACES:
        value = payload.get(key)
        available = bool(value)
        standing.append(
            {
                "surface": surface,
                "mode": "standing_evidence",
                "status": "available" if available else "unknown_unavailable",
                "source_key": key,
            }
        )

    return {
        "contract_version": "aidy_historical_toolbox_manifest_v1",
        "purpose": "tool_awareness_and_evidence_routing",
        "decision_rule": (
            "Consider every available standing evidence surface. Call an on-demand tool when "
            "its answer could materially change take/reduce/reject. Do not call tools merely "
            "to satisfy coverage, and never infer unavailable evidence."
        ),
        "on_demand_tools": [
            {
                "name": "get_recent_candles",
                "status": "available",
                "evidence_tier": "retrospective_research_m1",
                "use_when": "local price structure or trend label is insufficient",
            },
            {
                "name": CALENDAR_TOOL_NAME,
                "status": "available",
                "evidence_tier": "retrospective_official_schedule",
                "use_when": "macro-event proximity could materially change holding risk",
            },
            {
                "name": HISTORICAL_EVIDENCE_TOOL_NAME,
                "status": "available",
                "evidence_tier": "case_standing_evidence",
                "use_when": "a standing evidence surface needs focused inspection",
            },
        ],
        "standing_surfaces": standing,
        "not_connected_research_surfaces": [
            {"surface": name, "status": "unknown_not_connected_to_historical_contract"}
            for name in _NOT_CONNECTED_RESEARCH_SURFACES
        ],
        "target_outcome_available": False,
        "research_only": True,
        "live_money_execution_allowed": False,
    }


def _impact_rank(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(value.strip().lower(), 0)


def _calendar_events_between(*, start: datetime, end: datetime) -> list[dict[str, Any]]:
    point = datetime(start.year, start.month, start.day, tzinfo=UTC)
    stop = datetime(end.year, end.month, end.day, tzinfo=UTC)
    events: dict[tuple[str, str], dict[str, Any]] = {}
    while point <= stop:
        for event in historical_schedule_for_day(point):
            key = (str(event.get("title") or ""), str(event.get("time_utc") or ""))
            events[key] = event
        point += timedelta(days=1)
    result = [
        deepcopy(event)
        for event in events.values()
        if start <= datetime.fromisoformat(str(event["time_utc"])).astimezone(UTC) <= end
    ]
    result.sort(key=lambda item: str(item["time_utc"]))
    return result


def build_historical_tool_executor(
    *,
    payload: dict[str, Any],
    signal_posted_at: datetime,
) -> Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]:
    signal_at = _utc(signal_posted_at)

    async def executor(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == CALENDAR_TOOL_NAME:
            try:
                hours_before = max(0, min(int(arguments["hours_before"]), 72))
                hours_after = max(0, min(int(arguments["hours_after"]), 72))
                min_impact = str(arguments["min_impact"]).lower()
            except (KeyError, TypeError, ValueError):
                return {"error": "invalid_arguments"}
            if min_impact not in {"low", "medium", "high"}:
                return {"error": "invalid_arguments"}

            start = signal_at - timedelta(hours=hours_before)
            end = signal_at + timedelta(hours=hours_after)
            minimum = _impact_rank(min_impact)
            events = [
                event
                for event in _calendar_events_between(start=start, end=end)
                if _impact_rank(str(event.get("impact") or "")) >= minimum
            ]
            return {
                "events": events,
                "window_start_utc": start.isoformat(),
                "window_end_utc": end.isoformat(),
                "evidence_tier": "retrospective_official_schedule",
                "exact_pit_claimed": False,
                "target_outcome_available": False,
                "research_only": True,
                "live_money_execution_allowed": False,
            }

        if name == HISTORICAL_EVIDENCE_TOOL_NAME:
            surface = str(arguments.get("surface") or "")
            mapping = dict(_STANDING_SURFACES)
            key = mapping.get(surface)
            if key is None:
                return {"error": "invalid_surface"}
            return {
                "surface": surface,
                "status": "available" if bool(payload.get(key)) else "unknown_unavailable",
                "evidence": deepcopy(payload.get(key)),
                "target_outcome_available": False,
                "research_only": True,
                "live_money_execution_allowed": False,
            }

        return {"error": "unknown_tool"}

    return executor


__all__ = [
    "HISTORICAL_CALENDAR_TOOL_SCHEMA",
    "HISTORICAL_EVIDENCE_TOOL_NAME",
    "HISTORICAL_EVIDENCE_TOOL_SCHEMA",
    "build_historical_tool_executor",
    "historical_toolbox_manifest",
]
