from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.aidy_context_client import AidyCanonicalContext, AidyContextClient


class ProviderContextJoinBlocked(RuntimeError):
    pass


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field}_timezone_required")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ProviderAidyContextJoin:
    signal_posted_at: datetime
    provider_profile_version_id: str
    provider_profile_version_no: int
    provider_profile_effective_at: datetime
    aidy: AidyCanonicalContext

    @property
    def point_in_time_clean(self) -> bool:
        return (
            self.provider_profile_effective_at <= self.signal_posted_at
            and self.aidy.context_as_of_utc <= self.signal_posted_at
            and self.aidy.requested_as_of_utc == self.signal_posted_at
        )


async def join_provider_to_aidy_context(
    *,
    client: AidyContextClient,
    signal_posted_at: datetime,
    provider_profile_pit_status: str,
    provider_profile_version_id: str | None,
    provider_profile_version_no: int | None,
    provider_profile_effective_at: datetime | None,
) -> ProviderAidyContextJoin:
    """Join two independent PIT boundaries without granting execution authority.

    Day 8 owns provider-profile provenance. Day 9 consumes only rows already resolved
    against that immutable history and resolves AIDY market context at the same signal
    timestamp. This helper intentionally does not persist the joined packet; Day 10 owns
    immutable per-signal context attachment.
    """

    signal_at = _utc(signal_posted_at, field="signal_posted_at")
    if provider_profile_pit_status != "resolved":
        raise ProviderContextJoinBlocked("provider_profile_not_pit_resolved")
    version_id = str(provider_profile_version_id or "").strip()
    if not version_id or provider_profile_version_no is None or provider_profile_version_no < 1:
        raise ProviderContextJoinBlocked("provider_profile_version_missing")
    if provider_profile_effective_at is None:
        raise ProviderContextJoinBlocked("provider_profile_effective_at_missing")
    profile_at = _utc(provider_profile_effective_at, field="provider_profile_effective_at")
    if profile_at > signal_at:
        raise ProviderContextJoinBlocked("provider_profile_future_leak")

    context = await client.fetch_context(as_of=signal_at)
    if context.context_as_of_utc > signal_at:
        raise ProviderContextJoinBlocked("aidy_context_future_leak")
    if context.requested_as_of_utc != signal_at:
        raise ProviderContextJoinBlocked("aidy_context_request_mismatch")
    if context.provenance.get("live_money_execution_allowed") is not False:
        raise ProviderContextJoinBlocked("aidy_context_execution_authority_forbidden")

    joined = ProviderAidyContextJoin(
        signal_posted_at=signal_at,
        provider_profile_version_id=version_id,
        provider_profile_version_no=int(provider_profile_version_no),
        provider_profile_effective_at=profile_at,
        aidy=context,
    )
    if not joined.point_in_time_clean:
        raise ProviderContextJoinBlocked("provider_aidy_join_not_pit_clean")
    return joined
