"""Day 36 proof that later provider updates target only broker-existing positions."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from uuid import UUID

from app.mt5_management_day27 import (
    Day27Mt5ManagementService,
    _Account,
    _LocalPosition,
)


USER_ID = UUID("11111111-1111-4111-8111-111111111111")
SIGNAL_ID = UUID("22222222-2222-4222-8222-222222222222")
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")
LOCAL_1 = UUID("44444444-4444-4444-8444-444444444441")
LOCAL_2 = UUID("44444444-4444-4444-8444-444444444442")
LOCAL_3 = UUID("44444444-4444-4444-8444-444444444443")


class _Cipher:
    def decrypt(self, value: bytes) -> str:
        assert value == b"encrypted"
        return "token"


class _Read:
    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        assert token == "token"
        assert account_id == "account"
        return "london"

    async def read_positions(self, *, token: str, account_id: str, region: str):
        # mapped-3 was closed manually in MT5 before the provider update arrived.
        return [
            {"id": "mapped-1", "openPrice": 4410, "stopLoss": 4380, "takeProfit": 4440},
            {"id": "mapped-2", "openPrice": 4410, "stopLoss": 4380, "takeProfit": 4450},
        ]


class _Trade:
    def __init__(self) -> None:
        self.modified: list[str] = []

    async def modify_position(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        assert stop_loss == 4400.0
        assert take_profit is None
        self.modified.append(position_id)


class _Service(Day27Mt5ManagementService):
    def __init__(self) -> None:
        self._session_factory = None
        self._cipher = _Cipher()
        self._read = _Read()
        self._trade = _Trade()
        self.positions = [
            _LocalPosition(LOCAL_1, 1, "mapped-1", None, "open", Decimal("4380"), Decimal("4440")),
            _LocalPosition(LOCAL_2, 2, "mapped-2", None, "open", Decimal("4380"), Decimal("4450")),
            _LocalPosition(LOCAL_3, 3, "mapped-3", None, "open", Decimal("4380"), Decimal("4460")),
        ]

    def _existing_success(self, owner_user_id: UUID, lifecycle_event_id: UUID):
        assert owner_user_id == USER_ID
        assert lifecycle_event_id == EVENT_ID
        return None

    def _load_event(self, event_id: UUID):
        assert event_id == EVENT_ID
        return {
            "signal_id": SIGNAL_ID,
            "aggregate_result": {
                "revised_instruction": {
                    "management_actions": [
                        {"type": "edit_stop_loss", "target": "all", "value": "4400"}
                    ]
                }
            },
        }

    def _load_account(self, owner_user_id: UUID):
        assert owner_user_id == USER_ID
        return _Account(
            local_id=UUID("55555555-5555-4555-8555-555555555555"),
            account_id="account",
            token_ciphertext=b"encrypted",
        )

    def _load_positions(self, signal_id: UUID, user_id: UUID):
        assert signal_id == SIGNAL_ID
        assert user_id == USER_ID
        return tuple(self.positions)

    def _reconcile_missing_positions(
        self,
        *,
        signal_id: UUID,
        user_id: UUID,
        broker_position_ids: set[str],
    ) -> int:
        assert broker_position_ids == {"mapped-1", "mapped-2"}
        changed = 0
        for index, position in enumerate(self.positions):
            if (
                position.status == "open"
                and position.broker_position_id is not None
                and position.broker_position_id not in broker_position_ids
            ):
                self.positions[index] = replace(position, status="closed")
                changed += 1
        return changed

    def _update_local_stop(self, position_id: UUID, stop_loss: Decimal) -> None:
        for index, position in enumerate(self.positions):
            if position.id == position_id:
                self.positions[index] = replace(position, stop_loss=stop_loss)

    def _audit_success(self, result, actions):  # noqa: ANN001
        return None

    def _audit_failure(self, **kwargs):  # noqa: ANN003
        raise AssertionError(f"unexpected management failure: {kwargs}")


def test_provider_update_modifies_only_positions_that_still_exist_at_broker() -> None:
    service = _Service()

    result = asyncio.run(
        service.execute_owner_demo_event(
            owner_user_id=USER_ID,
            lifecycle_event_id=EVENT_ID,
        )
    )

    assert service._trade.modified == ["mapped-1", "mapped-2"]
    assert "mapped-3" not in service._trade.modified
    assert service.positions[2].status == "closed"
    assert result.external_positions_reconciled == 1
    assert result.positions_modified == 2
    assert result.broker_actions_sent == 2
