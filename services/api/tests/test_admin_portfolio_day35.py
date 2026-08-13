import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.admin_portfolio_day35 import (
    Day35AdminPortfolioService,
    Day35PortfolioRow,
    Day35PortfolioView,
    _OpenRequirement,
)
from app.metaapi_gateway import MetaApiGatewayError


SOURCE_A = UUID("11111111-1111-4111-8111-111111111111")
SOURCE_B = UUID("22222222-2222-4222-8222-222222222222")
OWNER = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)


def _row(
    *,
    source_id: UUID,
    source_label: str,
    trader_stream: str | None,
    dimension_type: str,
    trades_open: int,
) -> Day35PortfolioRow:
    return Day35PortfolioRow(
        dimension_type=dimension_type,
        source_id=source_id,
        source_label=source_label,
        trader_stream=trader_stream,
        source_color_index=1,
        realized_cash_pnl=Decimal("12.50"),
        open_cash_pnl=None,
        open_cash_pnl_known=False,
        return_percent=Decimal("1.25"),
        trades_closed=3,
        trades_open=trades_open,
        wins=2,
        losses=1,
        breakeven=0,
        win_rate_percent=Decimal("66.67"),
        net_pips=Decimal("25.0"),
        rank=1,
    )


def _base_view() -> Day35PortfolioView:
    return Day35PortfolioView(
        period_key="today",
        period_label="Today",
        period_start=NOW.replace(hour=0),
        period_end=NOW,
        reference_user_id=OWNER,
        rows=(
            _row(
                source_id=SOURCE_A,
                source_label="TDC2",
                trader_stream=None,
                dimension_type="source",
                trades_open=2,
            ),
            _row(
                source_id=SOURCE_A,
                source_label="TDC2",
                trader_stream="Matthew",
                dimension_type="trader",
                trades_open=1,
            ),
            _row(
                source_id=SOURCE_B,
                source_label="Provider B",
                trader_stream=None,
                dimension_type="source",
                trades_open=0,
            ),
        ),
    )


class _FakeLivePortfolio(Day35AdminPortfolioService):
    def __init__(self) -> None:
        pass

    def read(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return _base_view()

    def _open_requirements(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return [
            _OpenRequirement(SOURCE_A, "Matthew", "open", "101"),
            _OpenRequirement(SOURCE_A, None, "open", "102"),
        ]

    async def _read_live_broker_position_profit(self, user_id):  # noqa: ANN001
        assert user_id == OWNER
        return {"101": Decimal("1.25"), "102": Decimal("-0.40")}


def test_live_floating_pnl_uses_only_mapped_broker_positions_and_keeps_trader_split() -> None:
    view = asyncio.run(_FakeLivePortfolio().read_with_live_open_pnl("today"))

    source = view.rows[0]
    trader = view.rows[1]
    closed_only = view.rows[2]

    assert source.open_cash_pnl_known is True
    assert source.open_cash_pnl == Decimal("0.85")
    assert trader.open_cash_pnl_known is True
    assert trader.open_cash_pnl == Decimal("1.25")
    assert closed_only.open_cash_pnl_known is True
    assert closed_only.open_cash_pnl == Decimal("0")
    assert view.open_cash_pnl_live is True
    assert view.open_cash_pnl_error_code is None
    assert view.broker_trade_action_created is False


class _FailedLivePortfolio(_FakeLivePortfolio):
    async def _read_live_broker_position_profit(self, user_id):  # noqa: ANN001
        raise MetaApiGatewayError("metaapi_timeout", retryable=True)


def test_live_read_failure_never_turns_open_pnl_into_false_zero() -> None:
    view = asyncio.run(_FailedLivePortfolio().read_with_live_open_pnl("today"))

    assert view.rows[0].open_cash_pnl is None
    assert view.rows[0].open_cash_pnl_known is False
    assert view.rows[1].open_cash_pnl is None
    assert view.rows[1].open_cash_pnl_known is False
    assert view.rows[2].open_cash_pnl == Decimal("0")
    assert view.rows[2].open_cash_pnl_known is True
    assert view.open_cash_pnl_live is False
    assert view.open_cash_pnl_error_code == "metaapi_timeout"
    assert view.broker_trade_action_created is False


class _PendingOnlyPortfolio(_FakeLivePortfolio):
    def _open_requirements(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return [_OpenRequirement(SOURCE_A, None, "pending", None)]

    async def _read_live_broker_position_profit(self, user_id):  # noqa: ANN001
        raise AssertionError("pending-only portfolio must not need a broker position read")


def test_pending_only_dimension_has_zero_floating_without_network_trade_side_effect() -> None:
    view = asyncio.run(_PendingOnlyPortfolio().read_with_live_open_pnl("today"))

    assert all(row.open_cash_pnl_known for row in view.rows)
    assert all(row.open_cash_pnl == Decimal("0") for row in view.rows)
    assert view.open_cash_pnl_live is True
    assert view.broker_trade_action_created is False
