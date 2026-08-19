"""Explicit active paper-run epoch for the Owner reference account.

Historical broker deals, positions, signals and audit events remain immutable evidence.
The active paper-testing UI/reporting view begins at ``SUPER_SIGNALS_PAPER_RESET_AT``
and uses ``SUPER_SIGNALS_PAPER_BASELINE_BALANCE`` as its virtual starting balance.

This module contains configuration only. It installs no monkey patches and performs no
broker mutation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

_DEFAULT_BASELINE = Decimal("1000")


@dataclass(frozen=True, slots=True)
class PaperRunEpoch:
    owner_user_id: UUID
    started_at: datetime
    baseline_balance: Decimal


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def paper_reset_at() -> datetime | None:
    raw = os.getenv("SUPER_SIGNALS_PAPER_RESET_AT", "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _utc(value)


def paper_owner_id() -> UUID | None:
    raw = os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip()
    try:
        return UUID(raw)
    except (TypeError, ValueError):
        return None


def paper_baseline_balance() -> Decimal:
    raw = os.getenv("SUPER_SIGNALS_PAPER_BASELINE_BALANCE", "1000").strip() or "1000"
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        return _DEFAULT_BASELINE
    if not value.is_finite() or value <= 0:
        return _DEFAULT_BASELINE
    return value


def active_paper_epoch(user_id: UUID) -> PaperRunEpoch | None:
    owner_id = paper_owner_id()
    started_at = paper_reset_at()
    if owner_id is None or started_at is None or user_id != owner_id:
        return None
    return PaperRunEpoch(
        owner_user_id=owner_id,
        started_at=started_at,
        baseline_balance=paper_baseline_balance(),
    )


def clamp_to_epoch(user_id: UUID, value: datetime) -> datetime:
    epoch = active_paper_epoch(user_id)
    value_utc = _utc(value)
    if epoch is None:
        return value_utc
    return max(value_utc, epoch.started_at)


__all__ = [
    "PaperRunEpoch",
    "active_paper_epoch",
    "clamp_to_epoch",
    "paper_baseline_balance",
    "paper_owner_id",
    "paper_reset_at",
]
