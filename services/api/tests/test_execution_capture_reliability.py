from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.execution_capture_reliability import _CaptureRetryMixin
from app.mt5_execution_day26 import Day26ExecutionError


@pytest.fixture(autouse=True)
def _bypass_superseded_pending_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_cancel(_self, **_kwargs):
        return ()

    monkeypatch.setattr(
        "app.execution_capture_reliability.SupersededPendingOrderGuard.cancel_before_signal",
        no_cancel,
    )


class _FailThenSucceed:
    def __init__(self, codes: list[str]) -> None:
        self.codes = list(codes)
        self.calls = 0

    async def execute_owner_demo_signal(self, **kwargs):
        self.calls += 1
        if self.codes:
            raise Day26ExecutionError(self.codes.pop(0))
        return SimpleNamespace(positions=(object(),), double_lot_applied=False)


class _RetryHarness(_CaptureRetryMixin, _FailThenSucceed):
    def __init__(self, codes: list[str], *, clean: bool = True) -> None:
        _FailThenSucceed.__init__(self, codes)
        self.clean = clean
        self.audits: list[dict] = []
        # The production mixin now constructs the superseded-pending guard before
        # entering its retry loop. These placeholders keep this isolated retry harness
        # focused on retry semantics; the guard call itself is stubbed by the fixture.
        self._session_factory = object()
        self._cipher = object()
        self._read_gateway = object()
        self._trade_gateway = object()

    def _prepare_clean_retry(self, user_id, signal_id):
        return self.clean

    def _audit(self, **kwargs):
        self.audits.append(kwargs)


def test_transient_metaapi_failure_retries_when_previous_attempt_is_proven_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.execution_capture_reliability.asyncio.sleep", no_sleep)
    harness = _RetryHarness(["metaapi_temporarily_unavailable"])
    result = asyncio.run(
        harness.execute_owner_demo_signal(
            owner_user_id=uuid4(),
            signal_id=uuid4(),
            risk_percent="1",
            double_lot_approved=True,
        )
    )
    assert len(result.positions) == 1
    assert harness.calls == 2
    assert harness.audits[0]["payload"]["automatic_retry"] is True
    assert harness.audits[0]["payload"]["broker_mutation_present"] is False


def test_transient_failure_never_retries_when_state_is_not_proven_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.execution_capture_reliability.asyncio.sleep", no_sleep)
    harness = _RetryHarness(["metaapi_timeout"], clean=False)
    with pytest.raises(Day26ExecutionError, match="metaapi_timeout"):
        asyncio.run(
            harness.execute_owner_demo_signal(
                owner_user_id=uuid4(),
                signal_id=uuid4(),
                risk_percent="1",
                double_lot_approved=True,
            )
        )
    assert harness.calls == 1
    assert harness.audits == []


def test_hard_execution_error_is_never_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.execution_capture_reliability.asyncio.sleep", no_sleep)
    harness = _RetryHarness(["strict_directional_validation_failed"])
    with pytest.raises(Day26ExecutionError, match="strict_directional_validation_failed"):
        asyncio.run(
            harness.execute_owner_demo_signal(
                owner_user_id=uuid4(),
                signal_id=uuid4(),
                risk_percent="1",
                double_lot_approved=True,
            )
        )
    assert harness.calls == 1


def test_retry_is_bounded_even_for_repeated_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("app.execution_capture_reliability.asyncio.sleep", no_sleep)
    harness = _RetryHarness(["metaapi_unreachable"] * 5)
    with pytest.raises(Day26ExecutionError, match="metaapi_unreachable"):
        asyncio.run(
            harness.execute_owner_demo_signal(
                owner_user_id=uuid4(),
                signal_id=uuid4(),
                risk_percent="1",
                double_lot_approved=True,
            )
        )
    assert harness.calls == 4
    assert len(harness.audits) == 3
