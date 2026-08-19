"""Immutable active paper-run origin for the Owner reference account.

The accepted paper test began once, on 19 August 2026 at 11:12 Europe/Sofia
(08:12 UTC), from a virtual balance of USD 1,000. That origin is product truth, not a
runtime reset control. Deploys, restarts, environment changes and future dates must
never move it or erase post-origin performance.

Historical broker deals, positions, signals and audit events remain immutable forensic
truth. Owner dashboard/reporting views exclude pre-origin activity while every genuine
post-origin trade continues accumulating permanently.

This module installs no monkey patches and performs no broker mutation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

PAPER_RUN_STARTED_AT = datetime(2026, 8, 19, 8, 12, tzinfo=UTC)
PAPER_RUN_BASELINE_BALANCE = Decimal("1000")


@dataclass(frozen=True, slots=True)
class PaperRunEpoch:
    owner_user_id: UUID
    started_at: datetime
    baseline_balance: Decimal


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def paper_owner_id() -> UUID | None:
    raw = os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip()
    try:
        return UUID(raw)
    except (TypeError, ValueError):
        return None


def active_paper_epoch(user_id: UUID) -> PaperRunEpoch | None:
    owner_id = paper_owner_id()
    if owner_id is None or user_id != owner_id:
        return None
    return PaperRunEpoch(
        owner_user_id=owner_id,
        started_at=PAPER_RUN_STARTED_AT,
        baseline_balance=PAPER_RUN_BASELINE_BALANCE,
    )


def clamp_to_epoch(user_id: UUID, value: datetime) -> datetime:
    epoch = active_paper_epoch(user_id)
    value_utc = _utc(value)
    if epoch is None:
        return value_utc
    return max(value_utc, epoch.started_at)


__all__ = [
    "PAPER_RUN_BASELINE_BALANCE",
    "PAPER_RUN_STARTED_AT",
    "PaperRunEpoch",
    "active_paper_epoch",
    "clamp_to_epoch",
    "paper_owner_id",
]
