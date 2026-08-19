from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.execution_dispatch_canonical import CanonicalExecutionDispatcher, StoredDecision

OWNER = UUID("44444444-4444-4444-8444-444444444444")
SIGNAL = UUID("55555555-5555-4555-8555-555555555555")
EVENT = UUID("66666666-6666-4666-8666-666666666666")


class _MemberManagement:
    def __init__(self, *, target_count: int, succeeded: bool) -> None:
        self.target_count = target_count
        self.succeeded = succeeded

    async def distribute(self, **_: object):
        return SimpleNamespace(
            target_count=self.target_count,
            managed_count=1 if self.succeeded else 0,
            skipped_count=0 if self.succeeded else self.target_count,
            already_applied_count=0,
            any_management_succeeded=self.succeeded,
            outcomes=(),
        )


class _RouterHarness(CanonicalExecutionDispatcher):
    def __init__(self, *, member_target_count: int, member_succeeded: bool = False) -> None:
        self._owner_user_id = OWNER
        self._risk_percent = Decimal("1")
        self._double_lot_approved = True
        self._management = SimpleNamespace()
        self._member_management = _MemberManagement(
            target_count=member_target_count,
            succeeded=member_succeeded,
        )
        self._locks = {}
        self.audits: list[tuple[str, dict[str, object]]] = []

    def _resolve_lifecycle_event(self, message_id, revision_index):
        return EVENT, SIGNAL

    def _position_count(self, signal_id):
        return 0

    def _audit_success(self, **kwargs):
        self.audits.append(("success", kwargs))

    def _audit_failure(self, **kwargs):
        self.audits.append(("failure", kwargs))


def test_management_for_trade_never_opened_is_clean_noop() -> None:
    router = _RouterHarness(member_target_count=0)

    result = asyncio.run(
        router._dispatch_management(
            StoredDecision(uuid4(), "trade_update", "apply_update", "explicit_management"),
            0,
        )
    )

    assert result.outcome == "ignored"
    assert result.error_code is None
    assert result.broker_actions_sent == 0
    assert result.reason == "management_not_applicable_no_positions"
    assert not any(kind == "failure" for kind, _ in router.audits)
    success = next(payload for kind, payload in router.audits if kind == "success")
    assert success["payload"]["management_applicable"] is False
    assert success["payload"]["no_mapped_exposure"] is True


def test_real_management_target_with_no_success_still_blocks() -> None:
    router = _RouterHarness(member_target_count=1, member_succeeded=False)

    result = asyncio.run(
        router._dispatch_management(
            StoredDecision(uuid4(), "trade_update", "apply_update", "explicit_management"),
            0,
        )
    )

    assert result.outcome == "blocked"
    assert result.error_code == "no_user_management_succeeded"
    assert any(kind == "failure" for kind, _ in router.audits)
