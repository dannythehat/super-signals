from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

_CONTEXT_RETRY_ATTEMPTS = 4
_CONTEXT_RETRY_BASE_SECONDS = 0.25
_CONTEXT_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_GOLD_STATE_CONTRACTS = {"aidy_provider_gold_state_v1", "aidy_provider_gold_state_v2"}


def _utc_strict(value: datetime | str, *, field: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field}_timezone_required")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class AidyCanonicalContext:
    requested_as_of_utc: datetime
    context_as_of_utc: datetime
    context_lag_seconds: int
    context_hash: str
    snapshot_id: str
    snapshot_digest: str
    snapshot_archive_key: str
    session: dict[str, Any]
    regime: dict[str, Any]
    data_quality: dict[str, Any]
    market: dict[str, Any]
    provenance: dict[str, Any]
    gold_state: dict[str, Any] = field(default_factory=dict)


class AidyContextTerminalMiss(RuntimeError):
    """A fail-closed PIT miss that cannot become valid on a later retry."""

    def __init__(self, reason: str, *, payload: dict[str, Any]) -> None:
        self.reason = reason
        self.payload = dict(payload)
        super().__init__(reason)


class AidyContextUpstreamError(RuntimeError):
    """A retryable AIDY context endpoint failure with safe structured diagnostics."""

    def __init__(self, *, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = int(status_code)
        self.payload = dict(payload)
        error = str(payload.get("error") or "unknown_error")
        message = str(payload.get("message") or "").strip()
        detail = f"aidy_context_http_{self.status_code}:{error}"
        if message:
            detail = f"{detail}:{message[:500]}"
        super().__init__(detail)


class AidyContextClient:
    """Read-only client for AIDY's canonical point-in-time Provider Intelligence context."""

    def __init__(self, *, base_url: str, bearer_token: str, timeout_seconds: float = 12.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token.strip()
        if not self._bearer_token:
            raise ValueError("AIDY provider bearer token is required.")
        self._timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> AidyContextClient | None:
        base_url = os.getenv("AIDY_PROVIDER_MARKET_URL", "").strip()
        bearer_token = os.getenv("AIDY_PROVIDER_MARKET_TOKEN", "").strip()
        if not base_url or not bearer_token:
            return None
        return cls(base_url=base_url, bearer_token=bearer_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._bearer_token}",
            "Cache-Control": "no-cache",
            "User-Agent": "SuperSignals-ProviderLab-AIDY-Context/1.0",
        }

    async def _get_with_retry(self, *, url: str) -> httpx.Response:
        """Retry transient transport/server failures without weakening PIT validation."""
        timeout = httpx.Timeout(self._timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(_CONTEXT_RETRY_ATTEMPTS):
                try:
                    response = await client.get(url, headers=self._headers())
                except (
                    httpx.ConnectTimeout,
                    httpx.ConnectError,
                    httpx.ReadTimeout,
                    httpx.ReadError,
                    httpx.RemoteProtocolError,
                ):
                    if attempt + 1 >= _CONTEXT_RETRY_ATTEMPTS:
                        raise
                    await asyncio.sleep(_CONTEXT_RETRY_BASE_SECONDS * (2 ** attempt))
                    continue
                if (
                    response.status_code in _CONTEXT_RETRY_STATUS_CODES
                    and attempt + 1 < _CONTEXT_RETRY_ATTEMPTS
                ):
                    await asyncio.sleep(_CONTEXT_RETRY_BASE_SECONDS * (2 ** attempt))
                    continue
                return response
        raise RuntimeError("AIDY context retry bound exhausted.")

    async def fetch_context(self, *, as_of: datetime) -> AidyCanonicalContext:
        requested = _utc_strict(as_of, field="as_of")
        query = urlencode([("as_of", requested.isoformat())])
        response = await self._get_with_retry(
            url=f"{self._base_url}/provider/context?{query}"
        )
        error_payload: dict[str, Any] = {}
        if response.status_code >= 400:
            try:
                decoded = response.json()
            except ValueError:
                decoded = {}
            if isinstance(decoded, dict):
                error_payload = decoded
        terminal_reason: str | None = None
        if response.status_code == 409 and error_payload.get("error") == "pit_context_stale":
            terminal_reason = "pit_context_stale"
        elif response.status_code == 404 and error_payload.get("error") == "no_pit_context":
            # AIDY has no historical context at or before this immutable signal
            # timestamp. Retrying later cannot make a point-in-time snapshot appear
            # without rewriting history, so record a terminal research miss once.
            terminal_reason = "no_pit_context"
        if terminal_reason is not None:
            raise AidyContextTerminalMiss(
                terminal_reason,
                payload=error_payload,
            )
        if response.status_code >= 400:
            raise AidyContextUpstreamError(
                status_code=response.status_code,
                payload=error_payload,
            )
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError("AIDY context provider returned a non-success payload.")
        if payload.get("join_eligible") is not True:
            raise ValueError("aidy_context_not_join_eligible")
        echoed = _utc_strict(payload.get("requested_as_of_utc"), field="requested_as_of_utc")
        if echoed != requested:
            raise ValueError("aidy_context_request_timestamp_mismatch")
        context = payload.get("context")
        if not isinstance(context, dict):
            raise TypeError("aidy_context_payload_invalid")
        context_at = _utc_strict(context.get("context_as_of_utc"), field="context_as_of_utc")
        if context_at > requested:
            raise ValueError("aidy_context_future_leak")
        lag = int(payload.get("context_lag_seconds") or 0)
        max_lag = int(payload.get("max_context_lag_seconds") or 0)
        if lag < 0 or max_lag <= 0 or lag > max_lag:
            raise ValueError("aidy_context_lag_invalid")
        if str(context.get("symbol") or "") != "XAUUSD":
            raise ValueError("aidy_context_symbol_mismatch")
        provenance = context.get("provenance")
        if not isinstance(provenance, dict):
            raise TypeError("aidy_context_provenance_invalid")
        if provenance.get("private_forward_only") is not True:
            raise ValueError("aidy_context_not_private_forward")
        if provenance.get("live_money_execution_allowed") is not False:
            raise ValueError("aidy_context_illegal_execution_authority")
        snapshot = context.get("snapshot")
        if not isinstance(snapshot, dict):
            raise TypeError("aidy_context_snapshot_invalid")
        snapshot_at = _utc_strict(snapshot.get("captured_at_utc"), field="snapshot_captured_at_utc")
        if snapshot_at != context_at or snapshot_at > requested:
            raise ValueError("aidy_context_snapshot_time_mismatch")
        if str(snapshot.get("capture_status") or "") != "complete":
            raise ValueError("aidy_context_snapshot_not_complete")
        if str(snapshot.get("market_data_source") or "") != "twelve_data":
            raise ValueError("aidy_context_source_mismatch")
        digest = str(snapshot.get("snapshot_digest") or "").strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("aidy_context_snapshot_digest_invalid")
        archive_key = str(snapshot.get("archive_key") or "").strip()
        context_hash = str(context.get("context_hash") or "").strip()
        if not archive_key or not context_hash:
            raise ValueError("aidy_context_immutable_identity_missing")
        session = context.get("session")
        regime = context.get("regime")
        data_quality = context.get("data_quality")
        market = context.get("market")
        gold_state = context.get("gold_state")
        if not all(isinstance(value, dict) for value in (session, regime, data_quality, market)):
            raise TypeError("aidy_context_semantic_sections_invalid")
        if gold_state is None:
            gold_state = {}
        if not isinstance(gold_state, dict):
            raise TypeError("aidy_context_gold_state_invalid")
        if gold_state:
            contract_version = gold_state.get("contract_version")
            if contract_version not in _GOLD_STATE_CONTRACTS:
                raise ValueError("aidy_context_gold_state_contract_invalid")
            if gold_state.get("descriptive_context_only") is not True:
                raise ValueError("aidy_context_gold_state_not_descriptive_only")
            if gold_state.get("live_money_execution_allowed") is not False:
                raise ValueError("aidy_context_gold_state_illegal_execution_authority")
            if gold_state.get("predictive_edge_claimed") is not False:
                raise ValueError("aidy_context_gold_state_predictive_claim_forbidden")
            gold_state_at = _utc_strict(gold_state.get("as_of_utc"), field="gold_state_as_of_utc")
            if gold_state_at != context_at or gold_state_at > requested:
                raise ValueError("aidy_context_gold_state_time_mismatch")
            if contract_version == "aidy_provider_gold_state_v2":
                if gold_state.get("gold_state_engine_version") != "aidy_gold_state_engine_v1":
                    raise ValueError("aidy_context_gold_state_engine_version_invalid")
                if gold_state.get("research_only") is not True:
                    raise ValueError("aidy_context_gold_state_research_only_required")
                if gold_state.get("future_values_used") is not False:
                    raise ValueError("aidy_context_gold_state_future_values_forbidden")
                if gold_state.get("unknown_stays_unknown") is not True:
                    raise ValueError("aidy_context_gold_state_unknown_contract_invalid")
                digest = str(gold_state.get("gold_state_digest") or "")
                if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                    raise ValueError("aidy_context_gold_state_digest_invalid")
                for section_name in (
                    "session",
                    "market_structure",
                    "liquidity",
                    "location",
                    "volatility",
                    "scheduled_event_risk",
                    "move_observation",
                ):
                    if not isinstance(gold_state.get(section_name), dict):
                        raise TypeError(f"aidy_context_gold_state_{section_name}_invalid")
                liquidity = gold_state["liquidity"]
                if liquidity.get("proxy_not_order_flow") is not True:
                    raise ValueError("aidy_context_gold_state_liquidity_proxy_flag_missing")
                if liquidity.get("hidden_order_flow_claimed") is not False:
                    raise ValueError("aidy_context_gold_state_hidden_order_flow_forbidden")
                move = gold_state["move_observation"]
                if move.get("causal_attribution_proven") is not False:
                    raise ValueError("aidy_context_gold_state_causal_attribution_forbidden")
            research = gold_state.get("research_surfaces")
            if not isinstance(research, dict):
                raise TypeError("aidy_context_gold_state_research_invalid")
            if any(
                isinstance(item, dict) and item.get("decision_input_allowed") is not False
                for key, item in research.items()
                if key in {"rates_macro", "tiered_macro_events", "cme_contract_state"}
            ):
                raise ValueError("aidy_context_unqualified_research_surface_enabled")
        return AidyCanonicalContext(
            requested_as_of_utc=requested,
            context_as_of_utc=context_at,
            context_lag_seconds=lag,
            context_hash=context_hash,
            snapshot_id=str(snapshot.get("id") or ""),
            snapshot_digest=digest,
            snapshot_archive_key=archive_key,
            session=dict(session),
            regime=dict(regime),
            data_quality=dict(data_quality),
            market=dict(market),
            gold_state=dict(gold_state),
            provenance=dict(provenance),
        )
