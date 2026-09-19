from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.aidy_context_client import AidyContextClient

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0103_aidy_gold_state.py"


def _payload(*, illegal_research: bool = False) -> dict:
    stamp = "2026-09-18T16:00:00+00:00"
    return {
        "ok": True,
        "requested_as_of_utc": stamp,
        "context_lag_seconds": 0,
        "max_context_lag_seconds": 600,
        "join_eligible": True,
        "context": {
            "api_version": "aidy_provider_context_api_v3",
            "symbol": "XAUUSD",
            "context_as_of_utc": stamp,
            "context_hash": "c" * 64,
            "snapshot": {
                "id": "snapshot-1",
                "captured_at_utc": stamp,
                "capture_status": "complete",
                "market_data_source": "twelve_data",
                "snapshot_digest": "a" * 64,
                "archive_key": "gold/snapshot.json",
            },
            "session": {"computed_session_code": "new_york"},
            "regime": {"labels": {"trend_structure": "mixed"}},
            "data_quality": {"quote_freshness": "fresh"},
            "market": {"quote_context": {"mid": "4380"}},
            "gold_state": {
                "contract_version": "aidy_provider_gold_state_v2",
                "as_of_utc": stamp,
                "symbol": "XAUUSD",
                "descriptive_context_only": True,
                "predictive_edge_claimed": False,
                "live_money_execution_allowed": False,
                "price_liquidity": {
                    "state": "known",
                    "decision_input_allowed": True,
                    "structure": {"prior_day_breakout": {"state": "none"}},
                },
                "volatility": {
                    "state": "partial",
                    "decision_input_allowed": True,
                    "realized_volatility": {"state": "known"},
                },
                "scheduled_event_risk": {
                    "state": "unknown",
                    "decision_input_allowed": False,
                },
                "research_surfaces": {
                    "rates_macro": {
                        "state": "unknown",
                        "decision_input_allowed": illegal_research,
                    },
                    "tiered_macro_events": {
                        "state": "unknown",
                        "decision_input_allowed": False,
                    },
                    "cme_contract_state": {
                        "state": "unknown",
                        "decision_input_allowed": False,
                    },
                },
                "unknown_stays_unknown": True,
            },
            "provenance": {
                "private_forward_only": True,
                "live_money_execution_allowed": False,
            },
        },
    }


class _Response:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _AsyncClient:
    payload: dict = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        return _Response(self.payload)


def test_gold_state_migration_is_forward_only_and_bounded() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0103_aidy_gold_state"' in source
    assert 'down_revision: str | None = "0102_aidy_evidence_claims"' in source
    assert len("0103_aidy_gold_state") <= 32
    assert "gold_state_json jsonb NOT NULL" in source


def test_context_client_accepts_qualified_gold_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.aidy_context_client as module

    _AsyncClient.payload = _payload()
    monkeypatch.setattr(module.httpx, "AsyncClient", _AsyncClient)
    client = AidyContextClient(base_url="https://aidy.test", bearer_token="token")

    context = asyncio.run(
        client.fetch_context(as_of=datetime(2026, 9, 18, 16, 0, tzinfo=UTC))
    )

    assert context.gold_state["contract_version"] == "aidy_provider_gold_state_v2"
    assert context.gold_state["price_liquidity"]["decision_input_allowed"] is True
    assert context.gold_state["research_surfaces"]["rates_macro"]["decision_input_allowed"] is False


def test_context_client_rejects_unqualified_research_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.aidy_context_client as module

    _AsyncClient.payload = _payload(illegal_research=True)
    monkeypatch.setattr(module.httpx, "AsyncClient", _AsyncClient)
    client = AidyContextClient(base_url="https://aidy.test", bearer_token="token")

    with pytest.raises(ValueError, match="aidy_context_unqualified_research_surface_enabled"):
        asyncio.run(
            client.fetch_context(as_of=datetime(2026, 9, 18, 16, 0, tzinfo=UTC))
        )


def test_context_client_keeps_v2_rollout_backward_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.aidy_context_client as module

    payload = _payload()
    payload["context"].pop("gold_state")
    payload["context"]["api_version"] = "aidy_provider_context_api_v2"
    _AsyncClient.payload = payload
    monkeypatch.setattr(module.httpx, "AsyncClient", _AsyncClient)
    client = AidyContextClient(base_url="https://aidy.test", bearer_token="token")

    context = asyncio.run(
        client.fetch_context(as_of=datetime(2026, 9, 18, 16, 0, tzinfo=UTC))
    )

    assert context.gold_state == {}


def test_context_client_keeps_v1_gold_state_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.aidy_context_client as module

    payload = _payload()
    payload["context"]["gold_state"]["contract_version"] = "aidy_provider_gold_state_v1"
    _AsyncClient.payload = payload
    monkeypatch.setattr(module.httpx, "AsyncClient", _AsyncClient)
    client = AidyContextClient(base_url="https://aidy.test", bearer_token="token")

    context = asyncio.run(
        client.fetch_context(as_of=datetime(2026, 9, 18, 16, 0, tzinfo=UTC))
    )

    assert context.gold_state["contract_version"] == "aidy_provider_gold_state_v1"


def test_context_client_rejects_gold_state_that_claims_directional_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.aidy_context_client as module

    payload = _payload()
    payload["context"]["gold_state"]["predictive_edge_claimed"] = True
    _AsyncClient.payload = payload
    monkeypatch.setattr(module.httpx, "AsyncClient", _AsyncClient)
    client = AidyContextClient(base_url="https://aidy.test", bearer_token="token")

    with pytest.raises(ValueError, match="aidy_context_gold_state_predictive_claim_forbidden"):
        asyncio.run(client.fetch_context(as_of=datetime(2026, 9, 18, 16, 0, tzinfo=UTC)))


def test_context_client_rejects_non_descriptive_gold_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.aidy_context_client as module

    payload = _payload()
    payload["context"]["gold_state"]["descriptive_context_only"] = False
    _AsyncClient.payload = payload
    monkeypatch.setattr(module.httpx, "AsyncClient", _AsyncClient)
    client = AidyContextClient(base_url="https://aidy.test", bearer_token="token")

    with pytest.raises(ValueError, match="aidy_context_gold_state_not_descriptive_only"):
        asyncio.run(client.fetch_context(as_of=datetime(2026, 9, 18, 16, 0, tzinfo=UTC)))
