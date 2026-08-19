from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.mt5_execution_day26 import Day26ExecutionError
from app.paper_fresh_start_execution import PaperFreshStartExecutionService


class _ExplodingMarginGateway:
    async def calculate_margin(self, **kwargs):
        raise AssertionError("Shared execution must not call a local aggregate margin calculator")


@pytest.mark.asyncio
async def test_shared_engine_does_not_preblock_large_layer_grid_on_free_margin() -> None:
    service = object.__new__(PaperFreshStartExecutionService)
    service._margin_gateway = _ExplodingMarginGateway()
    entries = tuple(
        SimpleNamespace(entry_index=index, order_type="buy_limit", price=Decimal(4347 - index))
        for index in range(1, 8)
    )
    sizings = {
        index: SimpleNamespace(volume=Decimal("0.15"), position_count=4)
        for index in range(1, 8)
    }

    await service._margin_preflight(
        token="token",
        account_id="account",
        region="london",
        symbol="XAUUSD",
        side="BUY",
        free_margin=Decimal("0.01"),
        entries=entries,
        sizings=sizings,
    )


@pytest.mark.asyncio
async def test_each_leg_keeps_original_sizing_and_no_capacity_truncation_is_introduced() -> None:
    service = object.__new__(PaperFreshStartExecutionService)
    service._margin_gateway = _ExplodingMarginGateway()
    entries = tuple(
        SimpleNamespace(entry_index=index, order_type="buy_limit", price=Decimal(4400 - index))
        for index in range(1, 10)
    )
    sizings = {
        index: SimpleNamespace(volume=Decimal(str(index)) / Decimal("100"), position_count=4)
        for index in range(1, 10)
    }

    await service._margin_preflight(
        token="token",
        account_id="account",
        region="london",
        symbol="XAUUSD",
        side="BUY",
        free_margin=Decimal("1"),
        entries=entries,
        sizings=sizings,
    )

    assert [sizings[index].volume for index in range(1, 10)] == [
        Decimal(index) / Decimal("100") for index in range(1, 10)
    ]


@pytest.mark.asyncio
async def test_invalid_zero_target_signal_still_fails_for_structure_not_balance() -> None:
    service = object.__new__(PaperFreshStartExecutionService)
    service._margin_gateway = _ExplodingMarginGateway()
    with pytest.raises(Day26ExecutionError, match="position_count_invalid"):
        await service._margin_preflight(
            token="token",
            account_id="account",
            region="london",
            symbol="XAUUSD",
            side="BUY",
            free_margin=Decimal("999999"),
            entries=(SimpleNamespace(entry_index=1, order_type="market", price=Decimal("4400")),),
            sizings={1: SimpleNamespace(volume=Decimal("0.01"), position_count=0)},
        )
