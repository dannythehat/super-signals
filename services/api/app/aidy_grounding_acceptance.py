"""Forward acceptance monitor for AIDY evidence-grounded reasoning.

This module is deliberately observational. It audits new evidence-v2 reasoning rows against
the frozen PIT evidence contract and records the result in a research-only ledger. It does
not gate, create, resize, close, or otherwise affect any broker/live-money action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.aidy_evidence_contract import (
    EVIDENCE_CONTRACT_VERSION,
    EvidenceClaimValidationError,
    assert_no_freeform_provider_history,
)

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 60
_DEFAULT_ACCEPTANCE_ROWS = 10
_DEFAULT_ACCEPTANCE_PROVIDERS = 2

_SELECT = text(
    """
    SELECT
        a.id,
        a.created_at,
        a.evidence_contract_version,
        a.claim_validation_status,
        a.unsupported_claim_count,
        a.provider_evidence_snapshot,
        a.provider_claim_refs,
        a.provider_profile_version_no,
        a.rationale,
        a.key_factors,
        a.shadow_action_reason,
        d.signal_posted_at,
        COALESCE(NULLIF(s.source_alias,''),s.chat_title,'UNKNOWN') AS provider_name,
        d.source_id
    FROM aidy_reasoning_annotations a
    JOIN aidy_decisions d ON d.id=a.decision_id
    JOIN sources s ON s.id=d.source_id
    WHERE a.evidence_contract_version=:contract_version
    ORDER BY a.created_at,a.id
    """
)

_INSERT = text(
    """
    INSERT INTO aidy_grounding_acceptance_runs (
        id, checked_at, contract_version, state,
        total_rows, clean_rows, invalid_rows,
        rows_with_provider_claims, distinct_providers,
        required_rows, required_providers,
        failure_details, research_only, live_money_execution_allowed
    ) VALUES (
        :id, :checked_at, :contract_version, :state,
        :total_rows, :clean_rows, :invalid_rows,
        :rows_with_provider_claims, :distinct_providers,
        :required_rows, :required_providers,
        CAST(:failure_details AS jsonb), true, false
    )
    """
)


@dataclass(frozen=True, slots=True)
class GroundingAcceptanceSummary:
    state: str
    total_rows: int
    clean_rows: int
    invalid_rows: int
    rows_with_provider_claims: int
    distinct_providers: int
    required_rows: int
    required_providers: int
    failure_details: tuple[dict[str, Any], ...]


class GroundingAuditError(ValueError):
    pass


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer; using %s", name, default)
        return default
    if value <= 0:
        logger.warning("%s must be positive; using %s", name, default)
        return default
    return value


def _utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        if not isinstance(value, str) or not value.strip():
            raise GroundingAuditError(f"{field}_missing")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise GroundingAuditError(f"{field}_naive")
    return parsed.astimezone(UTC)


def audit_grounded_row(row: dict[str, Any]) -> None:
    if row.get("evidence_contract_version") != EVIDENCE_CONTRACT_VERSION:
        raise GroundingAuditError("contract_version_mismatch")
    if row.get("claim_validation_status") != "passed":
        raise GroundingAuditError("claim_validation_not_passed")
    if int(row.get("unsupported_claim_count") or 0) != 0:
        raise GroundingAuditError("unsupported_claim_count_nonzero")

    snapshot = row.get("provider_evidence_snapshot")
    refs = row.get("provider_claim_refs")
    if not isinstance(snapshot, list):
        raise GroundingAuditError("provider_evidence_snapshot_not_array")
    if not isinstance(refs, list):
        raise GroundingAuditError("provider_claim_refs_not_array")
    if len(refs) != len(set(str(ref) for ref in refs)):
        raise GroundingAuditError("duplicate_provider_claim_ref")

    signal_at = _utc(row.get("signal_posted_at"), field="signal_posted_at")
    claim_by_id: dict[str, dict[str, Any]] = {}
    for claim in snapshot:
        if not isinstance(claim, dict):
            raise GroundingAuditError("provider_evidence_claim_not_object")
        claim_id = str(claim.get("id") or "")
        if not claim_id:
            raise GroundingAuditError("provider_evidence_claim_id_missing")
        if claim_id in claim_by_id:
            raise GroundingAuditError(f"duplicate_provider_evidence_claim:{claim_id}")
        if not claim.get("source") or not claim.get("path"):
            raise GroundingAuditError(f"provider_evidence_claim_provenance_missing:{claim_id}")
        claim_at = _utc(claim.get("as_of_utc"), field=f"claim_as_of_utc:{claim_id}")
        if claim_at > signal_at:
            raise GroundingAuditError(f"future_provider_evidence:{claim_id}")
        if str(claim.get("kind") or "").startswith("provider_"):
            sample_n = claim.get("sample_n")
            if sample_n is not None and int(sample_n) <= 0:
                raise GroundingAuditError(f"provider_evidence_nonpositive_sample:{claim_id}")
        claim_by_id[claim_id] = claim

    for raw_ref in refs:
        ref = str(raw_ref)
        if ref not in claim_by_id:
            raise GroundingAuditError(f"provider_claim_ref_missing_from_snapshot:{ref}")

    provider_profile_version_no = row.get("provider_profile_version_no")
    for ref in refs:
        claim = claim_by_id[str(ref)]
        if claim.get("source") == "provider_profile":
            if provider_profile_version_no is None:
                raise GroundingAuditError("provider_profile_ref_without_row_version")
            if str(claim.get("version")) != str(provider_profile_version_no):
                raise GroundingAuditError(f"provider_profile_version_mismatch:{ref}")

    try:
        assert_no_freeform_provider_history(
            provider_name=str(row.get("provider_name") or ""),
            rationale=str(row.get("rationale") or ""),
            key_factors=[str(value) for value in (row.get("key_factors") or [])],
            action_reason=str(row.get("shadow_action_reason") or ""),
        )
    except EvidenceClaimValidationError as exc:
        raise GroundingAuditError(str(exc)) from exc


class AidyGroundingAcceptanceService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        required_rows: int | None = None,
        required_providers: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._required_rows = required_rows or _positive_int(
            "AIDY_GROUNDING_ACCEPTANCE_ROWS", _DEFAULT_ACCEPTANCE_ROWS
        )
        self._required_providers = required_providers or _positive_int(
            "AIDY_GROUNDING_ACCEPTANCE_PROVIDERS", _DEFAULT_ACCEPTANCE_PROVIDERS
        )

    def run(self) -> GroundingAcceptanceSummary:
        with self._session_factory() as session:
            rows = [dict(row) for row in session.execute(
                _SELECT, {"contract_version": EVIDENCE_CONTRACT_VERSION}
            ).mappings()]

        failures: list[dict[str, Any]] = []
        clean = 0
        rows_with_claims = 0
        providers: set[str] = set()
        for row in rows:
            providers.add(str(row.get("source_id") or ""))
            refs = row.get("provider_claim_refs")
            if isinstance(refs, list) and refs:
                rows_with_claims += 1
            try:
                audit_grounded_row(row)
                clean += 1
            except (GroundingAuditError, TypeError, ValueError) as exc:
                failures.append(
                    {
                        "annotation_id": str(row.get("id") or ""),
                        "created_at": (
                            row["created_at"].isoformat()
                            if isinstance(row.get("created_at"), datetime)
                            else str(row.get("created_at") or "")
                        ),
                        "reason": str(exc)[:240],
                    }
                )

        total = len(rows)
        if failures:
            state = "failed"
        elif total == 0:
            state = "waiting_forward_rows"
        elif total < self._required_rows or len(providers) < self._required_providers:
            state = "clean_so_far"
        else:
            state = "accepted"

        summary = GroundingAcceptanceSummary(
            state=state,
            total_rows=total,
            clean_rows=clean,
            invalid_rows=len(failures),
            rows_with_provider_claims=rows_with_claims,
            distinct_providers=len(providers),
            required_rows=self._required_rows,
            required_providers=self._required_providers,
            failure_details=tuple(failures[:25]),
        )
        self._persist(summary)
        return summary

    def _persist(self, summary: GroundingAcceptanceSummary) -> None:
        with self._session_factory() as session:
            session.execute(
                _INSERT,
                {
                    "id": str(uuid4()),
                    "checked_at": datetime.now(UTC),
                    "contract_version": EVIDENCE_CONTRACT_VERSION,
                    "state": summary.state,
                    "total_rows": summary.total_rows,
                    "clean_rows": summary.clean_rows,
                    "invalid_rows": summary.invalid_rows,
                    "rows_with_provider_claims": summary.rows_with_provider_claims,
                    "distinct_providers": summary.distinct_providers,
                    "required_rows": summary.required_rows,
                    "required_providers": summary.required_providers,
                    "failure_details": json.dumps(summary.failure_details, default=str),
                },
            )
            session.commit()


class AidyGroundingAcceptanceRuntime:
    """Continuously audit forward grounding evidence without touching execution."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        interval_seconds: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds or _positive_int(
            "AIDY_GROUNDING_ACCEPTANCE_INTERVAL_SECONDS", _DEFAULT_INTERVAL_SECONDS
        )
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> bool:
        if self.running:
            return True
        if os.getenv("AIDY_GROUNDING_ACCEPTANCE_ENABLED", "1").strip() == "0":
            logger.info("AIDY grounding acceptance monitor disabled by configuration")
            return False
        self._stopping.clear()
        service = AidyGroundingAcceptanceService(self._session_factory)
        self._task = asyncio.create_task(
            self._run(service), name="super-signals-aidy-grounding-acceptance"
        )
        logger.info(
            "AIDY grounding acceptance monitor started interval=%ss",
            self._interval_seconds,
        )
        return True

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._stopping.set()
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self, service: AidyGroundingAcceptanceService) -> None:
        while not self._stopping.is_set():
            try:
                summary = await asyncio.to_thread(service.run)
                log = logger.error if summary.state == "failed" else logger.info
                log(
                    "AIDY grounding acceptance state=%s total=%s clean=%s invalid=%s "
                    "provider_claim_rows=%s providers=%s/%s required_rows=%s",
                    summary.state,
                    summary.total_rows,
                    summary.clean_rows,
                    summary.invalid_rows,
                    summary.rows_with_provider_claims,
                    summary.distinct_providers,
                    summary.required_providers,
                    summary.required_rows,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - monitor must never affect the app
                logger.exception("AIDY grounding acceptance monitor failed safely")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue


__all__ = [
    "AidyGroundingAcceptanceRuntime",
    "AidyGroundingAcceptanceService",
    "GroundingAcceptanceSummary",
    "GroundingAuditError",
    "audit_grounded_row",
]
