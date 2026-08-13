"""Behavioral proof that Day 35 admin controls inherit Day 31 mapped-only closure."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.trading_controls_day31 import (
    Day31TradingControlService,
    Day31TradingControlView,
    _Account,
)


USER_ID = UUID("11111111-1111-4111-8111-111111111111")
LOCAL_1 = UUID("22222222-2222-4222-8222-222222222222")
LOCAL_2 = UUID("33333333-3333-4333-8333-333333333333")


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return False

    def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None

    def add(self, value):  # noqa: ANN001
        return None

    def commit(self):
        return None


class _Sessions:
    def __call__(self):
        return _Session()


class _Cipher:
    def decrypt(self, value):  # noqa: ANN001
        assert value == b"encrypted"
        return "token"


class _Read:
    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        assert token == "token"
        assert account_id == "account"
        return "london"

    async def read_positions(self, *, token: str, account_id: str, region: str):
        assert token == "token"
        assert account_id == "account"
        assert region == "london"
        # The broker contains two Super Signals positions AND one unrelated manual
        # position. Only the two IDs already mapped locally are eligible for close.
        return [
            {"id": "mapped-1", "symbol": "XAUUSD"},
            {"id": "manual-999", "symbol": "XAUUSD"},
            {"id": "mapped-2", "symbol": "XAUUSD"},
        ]


class _Trade:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def close_position(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
    ) -> None:
        assert token == "token"
        assert account_id == "account"
        assert region == "london"
        self.closed.append(position_id)


class _MappedOnlyService(Day31TradingControlService):
    def __init__(self) -> None:
        self._session_factory = _Sessions()
        self._cipher = _Cipher()
        self._read = _Read()
        self._trade = _Trade()
        self.marked_closed: list[tuple[UUID, str]] = []

    @staticmethod
    def _view() -> Day31TradingControlView:
        now = datetime(2026, 8, 13, tzinfo=UTC)
        return Day31TradingControlView(
            user_id=USER_ID,
            risk_percent=Decimal("1"),
            allow_double_lot=True,
            effective_normal_risk_percent=Decimal("1"),
            effective_double_lot_risk_percent=Decimal("2"),
            trading_status="active",
            activated_at=now,
            stopped_at=None,
            updated_at=now,
        )

    def get_settings(self, user_id: UUID) -> Day31TradingControlView:
        assert user_id == USER_ID
        return self._view()

    def _load_control(self, user_id: UUID) -> Day31TradingControlView:
        assert user_id == USER_ID
        view = self._view()
        return Day31TradingControlView(
            user_id=view.user_id,
            risk_percent=view.risk_percent,
            allow_double_lot=view.allow_double_lot,
            effective_normal_risk_percent=view.effective_normal_risk_percent,
            effective_double_lot_risk_percent=view.effective_double_lot_risk_percent,
            trading_status="stopped",
            activated_at=view.activated_at,
            stopped_at=datetime(2026, 8, 13, 1, tzinfo=UTC),
            updated_at=datetime(2026, 8, 13, 1, tzinfo=UTC),
        )

    def _load_open_mapped_positions(self, user_id: UUID):
        assert user_id == USER_ID
        return [(LOCAL_1, "mapped-1"), (LOCAL_2, "mapped-2")]

    def _connected_live_account(self, user_id: UUID):
        assert user_id == USER_ID
        return _Account(
            local_id=UUID("44444444-4444-4444-8444-444444444444"),
            account_id="account",
            login="123456",
            server="VantageInternational-Live",
            token_ciphertext=b"encrypted",
        )

    def _mark_closed(self, position_id: UUID, close_reason: str) -> None:
        self.marked_closed.append((position_id, close_reason))

    def _audit_stop_failure(self, user_id: UUID, error_code: str) -> None:
        raise AssertionError(f"unexpected stop failure {user_id} {error_code}")


def test_stop_closes_only_mapped_positions_even_when_manual_same_symbol_exists() -> None:
    service = _MappedOnlyService()

    result = asyncio.run(service.stop_and_close(USER_ID))

    assert service._trade.closed == ["mapped-1", "mapped-2"]
    assert "manual-999" not in service._trade.closed
    assert result.broker_actions_sent == 2
    assert result.positions_closed == 2
    assert result.external_positions_reconciled == 0
    assert result.settings.trading_status == "stopped"
    assert service.marked_closed == [
        (LOCAL_1, "user_stop_close"),
        (LOCAL_2, "user_stop_close"),
    ]
