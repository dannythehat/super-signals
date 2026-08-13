"""Day 36 manual-MT5 reconciliation acceptance coverage."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.manual_reconciliation_day36 import (
    Day36ManualMt5ReconciliationService,
    _Account,
    _MappedPosition,
    manual_deal_channel,
)


USER_ID = UUID("11111111-1111-4111-8111-111111111111")
SIGNAL_ID = UUID("22222222-2222-4222-8222-222222222222")
P1 = UUID("33333333-3333-4333-8333-333333333331")
P2 = UUID("33333333-3333-4333-8333-333333333332")
P3 = UUID("33333333-3333-4333-8333-333333333333")


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"encrypted"
        return "token"


class _Gateway:
    def __init__(self, *, manual_reason: str = "DEAL_REASON_MOBILE") -> None:
        self.manual_reason = manual_reason

    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        assert token == "token"
        assert account_id == "account"
        return "london"

    async def read_positions(self, *, token: str, account_id: str, region: str):
        assert token == "token"
        assert account_id == "account"
        assert region == "london"
        return [
            {
                "id": "mapped-1",
                "stopLoss": 4401.25,
                "takeProfit": 4440.0,
                "updateTime": "2026-08-13T09:10:00Z",
            },
            {
                "id": "mapped-2",
                "stopLoss": 4390.0,
                "takeProfit": 4455.5,
                "updateTime": "2026-08-13T09:11:00Z",
            },
        ]

    async def read_deals_by_position(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
    ):
        assert position_id == "mapped-3"
        return [
            {
                "id": "close-3",
                "positionId": "mapped-3",
                "entryType": "DEAL_ENTRY_OUT",
                "reason": self.manual_reason,
                "price": 4410.4,
                "time": "2026-08-13T09:12:00Z",
            }
        ]


class _Day36Service(Day36ManualMt5ReconciliationService):
    def __init__(self, *, manual_reason: str = "DEAL_REASON_MOBILE") -> None:
        self._cipher = _Cipher()
        self._gateway = _Gateway(manual_reason=manual_reason)
        self._session_factory = None
        self.positions = [
            _MappedPosition(
                id=P1,
                signal_id=SIGNAL_ID,
                tp_index=1,
                broker_position_id="mapped-1",
                status="open",
                stop_loss=Decimal("4399"),
                take_profit=Decimal("4440"),
            ),
            _MappedPosition(
                id=P2,
                signal_id=SIGNAL_ID,
                tp_index=2,
                broker_position_id="mapped-2",
                status="open",
                stop_loss=Decimal("4390"),
                take_profit=Decimal("4450"),
            ),
            _MappedPosition(
                id=P3,
                signal_id=SIGNAL_ID,
                tp_index=3,
                broker_position_id="mapped-3",
                status="open",
                stop_loss=Decimal("4385"),
                take_profit=Decimal("4460"),
            ),
        ]
        self.events: list[tuple[str, UUID, str | None, str | None]] = []

    def _account(self, user_id: UUID):
        assert user_id == USER_ID
        return _Account(account_id="account", token_ciphertext=b"encrypted")

    def _mapped_positions(self, user_id: UUID):
        assert user_id == USER_ID
        return tuple(self.positions)

    def _record_price_change(
        self,
        *,
        user_id: UUID,
        position: _MappedPosition,
        field: str,
        old_value: Decimal | None,
        new_value: Decimal | None,
        occurred_at: datetime,
    ) -> bool:
        assert user_id == USER_ID
        assert occurred_at.tzinfo is not None
        index = next(i for i, item in enumerate(self.positions) if item.id == position.id)
        current = self.positions[index]
        if getattr(current, field) != old_value:
            return False
        self.positions[index] = replace(current, **{field: new_value})
        self.events.append((field, position.id, str(old_value) if old_value is not None else None, str(new_value) if new_value is not None else None))
        return True

    def _record_manual_close(
        self,
        *,
        user_id: UUID,
        position: _MappedPosition,
        deal,
        observed_at: datetime,
    ) -> bool:
        assert user_id == USER_ID
        assert observed_at.tzinfo is UTC
        if manual_deal_channel(deal.get("reason")) is None:
            return False
        index = next(i for i, item in enumerate(self.positions) if item.id == position.id)
        current = self.positions[index]
        if current.status != "open":
            return False
        self.positions[index] = replace(current, status="closed")
        self.events.append(("position_closed", position.id, "open", "closed"))
        return True

    def _backfill_manual_closes(self, user_id: UUID) -> int:
        assert user_id == USER_ID
        return 0

    def read_recent(self, user_id: UUID, *, limit: int = 12):
        assert user_id == USER_ID
        assert limit == 12
        return ()


def test_manual_reason_classification_is_strict() -> None:
    assert manual_deal_channel("DEAL_REASON_CLIENT") == "MT5 desktop"
    assert manual_deal_channel("DEAL_REASON_MOBILE") == "MT5 mobile"
    assert manual_deal_channel("DEAL_REASON_WEB") == "MT5 web"
    assert manual_deal_channel("DEAL_REASON_EXPERT") is None
    assert manual_deal_channel("DEAL_REASON_SL") is None
    assert manual_deal_channel("DEAL_REASON_TP") is None


def test_reconcile_records_sl_tp_and_manual_close_once_without_broker_action() -> None:
    service = _Day36Service()

    first = asyncio.run(service.reconcile(USER_ID))

    assert first.stop_loss_changes == 1
    assert first.take_profit_changes == 1
    assert first.manual_closes == 1
    assert first.broker_trade_action_created is False
    assert service.positions[0].stop_loss == Decimal("4401.25")
    assert service.positions[1].take_profit == Decimal("4455.5")
    assert service.positions[2].status == "closed"
    assert service.events == [
        ("stop_loss", P1, "4399", "4401.25"),
        ("take_profit", P2, "4450", "4455.5"),
        ("position_closed", P3, "open", "closed"),
    ]

    second = asyncio.run(service.reconcile(USER_ID))
    assert second.stop_loss_changes == 0
    assert second.take_profit_changes == 0
    assert second.manual_closes == 0
    assert len(service.events) == 3


def test_expert_close_is_not_mislabeled_as_manual() -> None:
    service = _Day36Service(manual_reason="DEAL_REASON_EXPERT")

    result = asyncio.run(service.reconcile(USER_ID))

    assert result.stop_loss_changes == 1
    assert result.take_profit_changes == 1
    assert result.manual_closes == 0
    assert service.positions[2].status == "open"
    assert all(event[0] != "position_closed" for event in service.events)
