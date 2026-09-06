from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx


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

    async def fetch_context(self, *, as_of: datetime) -> AidyCanonicalContext:
        requested = _utc_strict(as_of, field="as_of")
        query = urlencode([("as_of", requested.isoformat())])
        async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds)) as client:
            response = await client.get(
                f"{self._base_url}/provider/context?{query}",
                headers=self._headers(),
            )
            response.raise_for_status()
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
        if not all(isinstance(value, dict) for value in (session, regime, data_quality, market)):
            raise TypeError("aidy_context_semantic_sections_invalid")
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
            provenance=dict(provenance),
        )
