"""Broker-account truth hardening for performance and reconciliation.

The Day 33 ledger originally fetched history only by known broker position. That is
insufficient for account reconciliation because balance/credit/correction operations and
broker deals which failed to map to a local position can then be invisible. This module
keeps the immutable broker_deals ledger but synchronises the complete MT5 deal stream.

The first successful account-wide sync backfills the Super Signals account-evidence
period. Later syncs use a five-minute overlap after the newest stored broker deal so the
Home dashboard can stay current without repeatedly downloading the whole account history.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.performance_ledger_day33 import (
    Day33LedgerError,
    Day33SyncResult,
    _d,
    _parse_time,
    trader_stream_for,
)
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2


_INSTALLED = False
_CHECKPOINT_EVENT = "mt5.performance_full_history_backfilled"
_OVERLAP = timedelta(minutes=5)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _history_start(
    self: Day33PerformanceLedgerServiceV2,
    user_id: UUID,
    mt5_account_id: UUID,
) -> tuple[datetime, bool]:
    """Return broker-history start and whether this is the one-time full backfill."""
    with self._session_factory() as session:
        checkpoint = session.execute(
            text(
                """
                SELECT created_at
                FROM audit_events
                WHERE actor_user_id=:user_id
                  AND entity_type='mt5_account'
                  AND entity_id=:account_id
                  AND event_type=:event_type
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {
                "user_id": user_id,
                "account_id": mt5_account_id,
                "event_type": _CHECKPOINT_EVENT,
            },
        ).scalar_one_or_none()
        if checkpoint is not None:
            latest_deal = session.execute(
                text(
                    """
                    SELECT MAX(occurred_at)
                    FROM broker_deals
                    WHERE user_id=:user_id AND mt5_account_id=:account_id
                    """
                ),
                {"user_id": user_id, "account_id": mt5_account_id},
            ).scalar_one_or_none()
            anchor = latest_deal or checkpoint
            return _as_utc(anchor) - _OVERLAP, False

        first_snapshot = session.execute(
            text(
                """
                SELECT MIN(captured_at)
                FROM performance_account_snapshots
                WHERE user_id=:user_id AND mt5_account_id=:account_id
                """
            ),
            {"user_id": user_id, "account_id": mt5_account_id},
        ).scalar_one_or_none()
        first_position = session.execute(
            text(
                """
                SELECT MIN(COALESCE(opened_at,created_at))
                FROM positions
                WHERE user_id=:user_id AND broker_position_id IS NOT NULL
                """
            ),
            {"user_id": user_id},
        ).scalar_one_or_none()

    candidates = [value for value in (first_snapshot, first_position) if value is not None]
    if not candidates:
        return datetime.now(UTC) - timedelta(days=7), True
    return min(_as_utc(value) for value in candidates) - timedelta(seconds=2), True


def _mark_full_backfill(
    self: Day33PerformanceLedgerServiceV2,
    *,
    user_id: UUID,
    mt5_account_id: UUID,
    start_time: datetime,
    end_time: datetime,
    deal_count: int,
) -> None:
    """Write exactly one immutable marker after a successful account-wide backfill."""
    with self._session_factory() as session:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    actor_user_id,event_type,entity_type,entity_id,payload
                )
                SELECT
                    :user_id,:event_type,'mt5_account',:account_id,
                    CAST(:payload AS jsonb)
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM audit_events
                    WHERE actor_user_id=:user_id
                      AND event_type=:event_type
                      AND entity_type='mt5_account'
                      AND entity_id=:account_id
                )
                """
            ),
            {
                "user_id": user_id,
                "event_type": _CHECKPOINT_EVENT,
                "account_id": mt5_account_id,
                "payload": json.dumps(
                    {
                        "start_time": start_time.isoformat(),
                        "end_time": end_time.isoformat(),
                        "broker_deals_seen": deal_count,
                        "trade_action_created": False,
                    },
                    separators=(",", ":"),
                ),
            },
        )
        session.commit()


def _store_account_deals(
    self: Day33PerformanceLedgerServiceV2,
    *,
    user_id: UUID,
    mt5_account_id: UUID,
    payloads: list[dict[str, object]],
) -> int:
    mapped_rows = self._mapped_positions(user_id)
    by_broker_position = {
        str(row["broker_position_id"]): row
        for row in mapped_rows
        if row["broker_position_id"]
    }
    added = 0
    with self._session_factory() as session:
        for payload in payloads:
            broker_deal_id = str(payload.get("id") or "").strip()
            deal_type = str(payload.get("type") or "").strip()
            if not broker_deal_id or not deal_type:
                continue
            occurred_at = _parse_time(payload.get("time"))
            broker_position_id = str(payload.get("positionId") or "").strip() or None
            row = by_broker_position.get(str(broker_position_id)) if broker_position_id else None
            trader = (
                trader_stream_for(
                    str(row["source_alias"] or ""),
                    str(row["original_text"] or ""),
                )
                if row is not None
                else None
            )
            result = session.execute(
                text(
                    """
                    INSERT INTO broker_deals (
                        user_id,mt5_account_id,position_id,signal_id,source_id,trader_stream,
                        broker_deal_id,broker_position_id,broker_order_id,broker_client_id,
                        deal_type,entry_type,symbol,volume,price,profit,commission,swap,
                        occurred_at,broker_time,raw_payload
                    ) VALUES (
                        :user_id,:mt5_account_id,:position_id,:signal_id,:source_id,:trader_stream,
                        :broker_deal_id,:broker_position_id,:broker_order_id,:broker_client_id,
                        :deal_type,:entry_type,:symbol,:volume,:price,:profit,:commission,:swap,
                        :occurred_at,:broker_time,CAST(:raw_payload AS jsonb)
                    )
                    ON CONFLICT (mt5_account_id,broker_deal_id) DO UPDATE SET
                        position_id=COALESCE(broker_deals.position_id,EXCLUDED.position_id),
                        signal_id=COALESCE(broker_deals.signal_id,EXCLUDED.signal_id),
                        source_id=COALESCE(broker_deals.source_id,EXCLUDED.source_id),
                        trader_stream=COALESCE(broker_deals.trader_stream,EXCLUDED.trader_stream),
                        broker_position_id=COALESCE(broker_deals.broker_position_id,EXCLUDED.broker_position_id),
                        broker_order_id=COALESCE(broker_deals.broker_order_id,EXCLUDED.broker_order_id),
                        broker_client_id=COALESCE(broker_deals.broker_client_id,EXCLUDED.broker_client_id),
                        raw_payload=EXCLUDED.raw_payload
                    RETURNING (xmax = 0) AS inserted
                    """
                ),
                {
                    "user_id": user_id,
                    "mt5_account_id": mt5_account_id,
                    "position_id": (row["id"] if row is not None else None),
                    "signal_id": (row["signal_id"] if row is not None else None),
                    "source_id": (row["source_id"] if row is not None else None),
                    "trader_stream": trader,
                    "broker_deal_id": broker_deal_id,
                    "broker_position_id": broker_position_id,
                    "broker_order_id": str(payload.get("orderId") or "").strip() or None,
                    "broker_client_id": str(payload.get("clientId") or "").strip() or None,
                    "deal_type": deal_type,
                    "entry_type": str(payload.get("entryType") or "").strip() or None,
                    "symbol": str(
                        payload.get("symbol")
                        or (row["symbol"] if row is not None else "")
                        or ""
                    ).strip()
                    or None,
                    "volume": (
                        _d(payload.get("volume"))
                        if payload.get("volume") is not None
                        else None
                    ),
                    "price": (
                        _d(payload.get("price"))
                        if payload.get("price") is not None
                        else None
                    ),
                    "profit": _d(payload.get("profit")),
                    "commission": _d(payload.get("commission")),
                    "swap": _d(payload.get("swap")),
                    "occurred_at": occurred_at,
                    "broker_time": str(payload.get("brokerTime") or "").strip() or None,
                    "raw_payload": json.dumps(payload, separators=(",", ":"), default=str),
                },
            ).first()
            if result is not None and bool(result[0]):
                added += 1
        session.commit()
    return added


async def _sync_user(
    self: Day33PerformanceLedgerServiceV2,
    user_id: UUID,
) -> Day33SyncResult:
    account = self._account(user_id)
    if account is None:
        raise Day33LedgerError("mt5_account_not_configured")
    if str(account["status"]) != "connected":
        raise Day33LedgerError("mt5_account_not_connected", retryable=True)
    try:
        token = self._cipher.decrypt(bytes(account["metaapi_token_ciphertext"]))
    except BrokerCredentialDecryptionError as exc:
        raise Day33LedgerError("broker_credential_decryption_failed") from exc

    account_id = str(account["metaapi_account_id"])
    try:
        region = await self._gateway.resolve_account_region(token=token, account_id=account_id)
        account_payload = await self._gateway.read_account_information(
            token=token,
            account_id=account_id,
            region=region,
        )
    except MetaApiGatewayError as exc:
        raise Day33LedgerError(exc.code, retryable=exc.retryable) from exc

    self._backfill_account_snapshots_from_audit(
        user_id=user_id,
        mt5_account_id=account["id"],
    )
    captured_at = datetime.now(UTC)
    self._store_snapshot(
        user_id=user_id,
        mt5_account_id=account["id"],
        payload=account_payload,
        captured_at=captured_at,
    )

    start_time, full_backfill = _history_start(self, user_id, account["id"])
    payloads: list[dict[str, object]] = []
    offset = 0
    page_size = 1000
    try:
        while True:
            page = await self._gateway.read_deals_by_time_range(
                token=token,
                account_id=account_id,
                region=region,
                start_time=start_time,
                end_time=captured_at,
                offset=offset,
                limit=page_size,
            )
            payloads.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
            if offset > 100_000:
                raise Day33LedgerError("broker_history_too_large")
    except MetaApiGatewayError as exc:
        if exc.code != "metaapi_terminal_data_unavailable":
            raise Day33LedgerError(exc.code, retryable=exc.retryable) from exc

    added = _store_account_deals(
        self,
        user_id=user_id,
        mt5_account_id=account["id"],
        payloads=payloads,
    )
    if full_backfill:
        _mark_full_backfill(
            self,
            user_id=user_id,
            mt5_account_id=account["id"],
            start_time=start_time,
            end_time=captured_at,
            deal_count=len(payloads),
        )

    mapped_positions = self._mapped_positions(user_id)
    outcomes = self.rebuild_outcomes(user_id)
    summaries = self.rebuild_summaries(user_id)
    return Day33SyncResult(
        user_id=user_id,
        broker_deals_added=added,
        mapped_positions_checked=len(mapped_positions),
        outcomes_rebuilt=outcomes,
        summaries_rebuilt=summaries,
        broker_trade_action_created=False,
    )


def _repair_timeline_rows(
    self: Day33PerformanceLedgerServiceV2,
    user_id: UUID,
) -> list[Any]:
    """Never show blank XAUUSD pips when broker entry/exit prices are known."""
    rows = list(_ORIGINAL_TIMELINE_ROWS(self, user_id))
    with self._session_factory() as session:
        repairs = {
            row["signal_id"]: row
            for row in session.execute(
                text(
                    """
                    SELECT
                        signal_id,
                        SUM(cash_pnl) FILTER (
                            WHERE status IN ('won','lost','breakeven')
                              AND cash_pnl IS NOT NULL
                        ) AS realised_pnl,
                        COUNT(*) FILTER (
                            WHERE status IN ('won','lost','breakeven')
                        )::int AS known_legs,
                        COUNT(*) FILTER (
                            WHERE status IN ('won','lost','breakeven')
                              AND COALESCE(
                                  net_pips,
                                  CASE
                                      WHEN UPPER(symbol)='XAUUSD'
                                       AND entry_price IS NOT NULL
                                       AND exit_price IS NOT NULL
                                      THEN CASE
                                          WHEN UPPER(side)='BUY' THEN (exit_price-entry_price)/0.1
                                          WHEN UPPER(side)='SELL' THEN (entry_price-exit_price)/0.1
                                          ELSE NULL
                                      END
                                      ELSE NULL
                                  END
                              ) IS NULL
                        )::int AS missing_pip_legs,
                        SUM(
                            COALESCE(
                                net_pips,
                                CASE
                                    WHEN UPPER(symbol)='XAUUSD'
                                     AND entry_price IS NOT NULL
                                     AND exit_price IS NOT NULL
                                    THEN CASE
                                        WHEN UPPER(side)='BUY' THEN (exit_price-entry_price)/0.1
                                        WHEN UPPER(side)='SELL' THEN (entry_price-exit_price)/0.1
                                        ELSE NULL
                                    END
                                    ELSE NULL
                                END
                            )
                        ) FILTER (WHERE status IN ('won','lost','breakeven')) AS repaired_pips
                    FROM performance_trade_outcomes
                    WHERE user_id=:user_id
                    GROUP BY signal_id
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
        }
    result: list[Any] = []
    for original in rows:
        item = dict(original)
        if str(item.get("status_override") or "") != "skipped":
            repair = repairs.get(item.get("signal_id"))
            if repair is not None:
                if repair["realised_pnl"] is not None:
                    item["cash_pnl"] = repair["realised_pnl"]
                if (
                    int(repair["known_legs"] or 0) > 0
                    and int(repair["missing_pip_legs"] or 0) == 0
                ):
                    item["net_pips"] = repair["repaired_pips"]
        result.append(item)
    return result


def account_reconciliation(
    self: Day33PerformanceLedgerServiceV2,
    user_id: UUID,
    *,
    start_time: datetime,
    end_time: datetime,
) -> dict[str, Decimal | bool | None]:
    """Reconcile broker balance movement to all stored account deals in a window."""
    with self._session_factory() as session:
        first = session.execute(
            text(
                """
                SELECT balance,captured_at
                FROM performance_account_snapshots
                WHERE user_id=:user_id
                  AND captured_at>=:start_time
                  AND captured_at<:end_time
                ORDER BY captured_at ASC
                LIMIT 1
                """
            ),
            {"user_id": user_id, "start_time": start_time, "end_time": end_time},
        ).mappings().first()
        last = session.execute(
            text(
                """
                SELECT balance,captured_at
                FROM performance_account_snapshots
                WHERE user_id=:user_id
                  AND captured_at>=:start_time
                  AND captured_at<:end_time
                ORDER BY captured_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id, "start_time": start_time, "end_time": end_time},
        ).mappings().first()
        if first is None or last is None or first["captured_at"] == last["captured_at"]:
            return {
                "opening_balance": None,
                "closing_balance": None,
                "balance_change": None,
                "trading_cash": Decimal("0"),
                "non_trade_cash": Decimal("0"),
                "reconciliation_gap": None,
                "reconciled": False,
            }
        deal_row = session.execute(
            text(
                """
                SELECT
                    COALESCE(SUM(profit+commission+swap) FILTER (
                        WHERE broker_position_id IS NOT NULL
                           OR symbol IS NOT NULL
                           OR UPPER(COALESCE(entry_type,'')) LIKE 'DEAL_ENTRY_%'
                    ),0) AS trading_cash,
                    COALESCE(SUM(profit+commission+swap) FILTER (
                        WHERE broker_position_id IS NULL
                          AND symbol IS NULL
                          AND UPPER(COALESCE(entry_type,'')) NOT LIKE 'DEAL_ENTRY_%'
                    ),0) AS non_trade_cash,
                    COALESCE(SUM(profit+commission+swap),0) AS all_cash
                FROM broker_deals
                WHERE user_id=:user_id
                  AND occurred_at>:first_at
                  AND occurred_at<=:last_at
                """
            ),
            {
                "user_id": user_id,
                "first_at": first["captured_at"],
                "last_at": last["captured_at"],
            },
        ).mappings().one()
    opening = _d(first["balance"])
    closing = _d(last["balance"])
    movement = closing - opening
    trading = _d(deal_row["trading_cash"])
    non_trade = _d(deal_row["non_trade_cash"])
    gap = movement - _d(deal_row["all_cash"])
    return {
        "opening_balance": opening,
        "closing_balance": closing,
        "balance_change": movement,
        "trading_cash": trading,
        "non_trade_cash": non_trade,
        "reconciliation_gap": gap,
        "reconciled": abs(gap) <= Decimal("0.01"),
    }


def install_performance_account_truth_override() -> None:
    global _INSTALLED, _ORIGINAL_TIMELINE_ROWS
    if _INSTALLED:
        return
    _ORIGINAL_TIMELINE_ROWS = Day33PerformanceLedgerServiceV2._timeline_rows
    Day33PerformanceLedgerServiceV2.sync_user = _sync_user  # type: ignore[method-assign]
    Day33PerformanceLedgerServiceV2._timeline_rows = _repair_timeline_rows  # type: ignore[method-assign]
    Day33PerformanceLedgerServiceV2.read_account_reconciliation = account_reconciliation  # type: ignore[attr-defined]
    _INSTALLED = True


_ORIGINAL_TIMELINE_ROWS = Day33PerformanceLedgerServiceV2._timeline_rows


__all__ = ["install_performance_account_truth_override"]
