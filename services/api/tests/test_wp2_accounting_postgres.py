"""Real SQL regressions against isolated temporary tables on a localhost test DB only."""

from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal as D
import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.performance_ledger_canonical import CanonicalPerformanceLedgerService
from app.broker_fill_settlement import BrokerFillSettlementService
from app.running_daily_balance import account_value_days


@pytest.fixture
def ledger():
    value = os.getenv("DATABASE_URL", "")
    if not value:
        pytest.skip("localhost test PostgreSQL not configured")
    url = make_url(value)
    if url.host not in ("localhost", "127.0.0.1") or not (url.database or "").endswith("_test"):
        pytest.skip("refusing a non-localhost or non-test database")
    engine = create_engine(url)
    with engine.connect() as connection, connection.begin():
        connection.execute(
            text(
                "CREATE TEMP TABLE performance_account_snapshots (user_id uuid, mt5_account_id uuid,currency text,balance numeric,equity numeric,captured_at timestamptz) ON COMMIT DROP"
            )
        )
        connection.execute(
            text(
                "CREATE TEMP TABLE broker_deals (user_id uuid,mt5_account_id uuid,broker_deal_id text,broker_position_id text,deal_type text,entry_type text,profit numeric,commission numeric,swap numeric,raw_payload jsonb,occurred_at timestamptz,volume numeric,price numeric) ON COMMIT DROP"
            )
        )
        session = Session(bind=connection)
        yield session, uuid4(), uuid4()
        session.close()
    engine.dispose()


def insert_deal(
    session,
    user,
    account,
    ticket,
    typ,
    profit,
    commission=0,
    swap=0,
    fee=0,
    entry="DEAL_ENTRY_IN",
    volume=".01",
):
    import json

    session.execute(
        text(
            "INSERT INTO broker_deals VALUES (:u,:a,:id,'fixture-position',:t,:e,:p,:c,:s,CAST(:raw AS jsonb),:at,:v,4100)"
        ),
        dict(
            u=user,
            a=account,
            id=ticket,
            t=typ,
            e=entry,
            p=profit,
            c=commission,
            s=swap,
            raw=json.dumps({"fee": fee}),
            at=datetime(2026, 11, 5, 20, tzinfo=UTC),
            v=volume,
        ),
    )


def test_daily_and_reconciliation_sql_preserve_cash_currency_and_floating(ledger):
    session, user, account = ledger
    for at, balance, equity in [
        (datetime(2026, 11, 5, 19, tzinfo=UTC), 10000, 10000),
        (datetime(2026, 11, 6, 4, tzinfo=UTC), 10844, 10854),
    ]:
        session.execute(
            text("INSERT INTO performance_account_snapshots VALUES (:u,:a,'USC',:b,:e,:t)"),
            dict(u=user, a=account, b=balance, e=equity, t=at),
        )
    insert_deal(session, user, account, "1", "DEAL_TYPE_BALANCE", 1000)
    insert_deal(session, user, account, "2", "DEAL_TYPE_BALANCE", -200)
    insert_deal(
        session, user, account, "3", "DEAL_TYPE_SELL", 50, -2, -3, -1, entry="DEAL_ENTRY_OUT"
    )
    day = account_value_days(session, user, now=datetime(2026, 11, 6, 4, tzinfo=UTC))[-1]
    assert day.realised_pnl == 44 and day.floating_pnl_change == 10 and day.pnl == 54
    assert (
        day.cash_movement == 800
        and day.currency == "USC"
        and day.reconciliation_gap == 0
        and day.charges_complete
    )
    svc = object.__new__(CanonicalPerformanceLedgerService)
    svc._session_factory = lambda: nullcontext(session)
    r = svc.read_account_reconciliation(
        user,
        start_time=datetime(2026, 11, 5, tzinfo=UTC),
        end_time=datetime(2026, 11, 7, tzinfo=UTC),
        mt5_account_id=account,
    )
    assert r["trading_cash"] == 44 and r["cash_movement"] == 800 and r["reconciled"]
    # Broker credit changes equity, not trading P&L or the cash balance.
    insert_deal(session, user, account, "4", "DEAL_TYPE_CREDIT", 20)
    session.execute(
        text("INSERT INTO performance_account_snapshots VALUES (:u,:a,'USC',10844,10874,:t)"),
        dict(u=user, a=account, t=datetime(2026, 11, 6, 4, 1, tzinfo=UTC)),
    )
    credited = account_value_days(session, user, now=datetime(2026, 11, 6, 4, 1, tzinfo=UTC))[-1]
    assert (
        credited.pnl == 54 and credited.credit_movement == 20 and credited.reconciliation_gap == 0
    )
    # A second currency/account cannot silently become a combined daily portfolio.
    session.execute(
        text("INSERT INTO performance_account_snapshots VALUES (:u,:a,'USD',500,500,:t)"),
        dict(u=user, a=uuid4(), t=datetime(2026, 11, 6, 3, tzinfo=UTC)),
    )
    with pytest.raises(ValueError, match="account_currency_scope_unavailable"):
        account_value_days(session, user, now=datetime(2026, 11, 6, 4, tzinfo=UTC))


def test_settlement_sql_includes_entry_fees_and_every_partial_exit(ledger):
    session, user, account = ledger
    insert_deal(session, user, account, "1", "DEAL_TYPE_BUY", 0, -2, 0, -0.5, volume=".10")
    insert_deal(
        session,
        user,
        account,
        "2",
        "DEAL_TYPE_SELL",
        40,
        -0.8,
        -1,
        -0.2,
        entry="DEAL_ENTRY_OUT",
        volume=".04",
    )
    insert_deal(
        session,
        user,
        account,
        "3",
        "DEAL_TYPE_SELL",
        60,
        -1.2,
        -2,
        -0.3,
        entry="DEAL_ENTRY_OUT",
        volume=".06",
    )
    svc = object.__new__(BrokerFillSettlementService)
    svc._session_factory = lambda: nullcontext(session)
    r = svc._evidence(mt5_account_id=account, broker_position_id="fixture-position")
    assert r.entry_volume == D(".10") and r.exit_volume == D(".10") and r.realised_cash == 92
