"""Canonical broker-account performance ledger.

The dashboard and settlement watcher use one broker-truth model:
* account snapshots come from MetaAPI account information;
* the complete MT5 deal stream is synchronised, not only already-known positions;
* broker financial facts/raw payload are immutable; missing local linkage metadata may be enriched only from one unambiguous mapped position;
* first sync backfills the available Super Signals evidence period, later syncs overlap
  five minutes from the newest stored deal;
* completed XAUUSD pips are derived from broker entry/exit prices when the stored pips
  field is absent;
* account balance movement can be reconciled to stored broker cash movements.
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

_CHECKPOINT_EVENT = "mt5.performance_full_history_backfilled"
_HISTORY_OVERLAP = timedelta(minutes=5)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _unique_position_index(rows: list[Any], field: str) -> dict[str, Any]:
    """Index only broker identifiers which resolve to exactly one local position."""
    index: dict[str, Any] = {}
    ambiguous: set[str] = set()
    for row in rows:
        value = str(row[field] or "").strip()
        if not value or value in ambiguous:
            continue
        existing = index.get(value)
        if existing is not None and str(existing["id"]) != str(row["id"]):
            index.pop(value, None)
            ambiguous.add(value)
            continue
        index[value] = row
    return index


def _resolve_mapped_position(
    *,
    broker_position_id: str | None,
    broker_order_id: str | None,
    broker_client_id: str | None,
    by_broker_position: dict[str, Any],
    by_broker_order: dict[str, Any],
    by_broker_client: dict[str, Any],
) -> Any | None:
    """Return one local position only when all available mappings agree."""
    matches: list[Any] = []
    for value, index in (
        (broker_position_id, by_broker_position),
        (broker_order_id, by_broker_order),
        (broker_client_id, by_broker_client),
    ):
        key = str(value or "").strip()
        if not key:
            continue
        candidate = index.get(key)
        if candidate is not None:
            matches.append(candidate)
    if not matches:
        return None
    ids = {str(row["id"]) for row in matches}
    return matches[0] if len(ids) == 1 else None


class CanonicalPerformanceLedgerService(Day33PerformanceLedgerServiceV2):
    """Single production performance/reconciliation service."""

    def _history_start(
        self,
        user_id: UUID,
        mt5_account_id: UUID,
    ) -> tuple[datetime, bool]:
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
                return _as_utc(anchor) - _HISTORY_OVERLAP, False

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
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        start_time: datetime,
        end_time: datetime,
        deal_count: int,
    ) -> None:
        payload = json.dumps(
            {
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "broker_deals_seen": deal_count,
                "trade_action_created": False,
            },
            separators=(",", ":"),
        )
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        actor_user_id,event_type,entity_type,entity_id,payload
                    )
                    SELECT
                        CAST(:user_id AS uuid),CAST(:event_type AS varchar),
                        CAST('mt5_account' AS varchar),CAST(:account_id AS uuid),
                        CAST(:payload AS jsonb)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM audit_events
                        WHERE actor_user_id=CAST(:user_id AS uuid)
                          AND event_type=CAST(:event_type AS varchar)
                          AND entity_type=CAST('mt5_account' AS varchar)
                          AND entity_id=CAST(:account_id AS uuid)
                    )
                    """
                ),
                {
                    "user_id": user_id,
                    "event_type": _CHECKPOINT_EVENT,
                    "account_id": mt5_account_id,
                    "payload": payload,
                },
            )
            session.commit()

    def _store_account_deals(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        payloads: list[dict[str, object]],
    ) -> int:
        mapped_rows = self._mapped_positions(user_id)
        by_broker_position = _unique_position_index(mapped_rows, "broker_position_id")
        by_broker_order = _unique_position_index(mapped_rows, "broker_order_id")
        by_broker_client = _unique_position_index(mapped_rows, "broker_client_id")
        added = 0
        with self._session_factory() as session:
            for payload in payloads:
                broker_deal_id = str(payload.get("id") or "").strip()
                deal_type = str(payload.get("type") or "").strip()
                if not broker_deal_id or not deal_type:
                    continue
                occurred_at = _parse_time(payload.get("time"))
                broker_position_id = str(payload.get("positionId") or "").strip() or None
                broker_order_id = str(payload.get("orderId") or "").strip() or None
                broker_client_id = str(payload.get("clientId") or "").strip() or None
                row = _resolve_mapped_position(
                    broker_position_id=broker_position_id,
                    broker_order_id=broker_order_id,
                    broker_client_id=broker_client_id,
                    by_broker_position=by_broker_position,
                    by_broker_order=by_broker_order,
                    by_broker_client=by_broker_client,
                )
                trader = (
                    trader_stream_for(
                        str(row["source_alias"] or ""),
                        str(row["original_text"] or ""),
                    )
                    if row is not None
                    else None
                )
                inserted = session.execute(
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
                        ON CONFLICT (mt5_account_id,broker_deal_id) DO NOTHING
                        RETURNING 1
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
                        "broker_order_id": broker_order_id,
                        "broker_client_id": broker_client_id,
                        "deal_type": deal_type,
                        "entry_type": str(payload.get("entryType") or "").strip() or None,
                        "symbol": str(
                            payload.get("symbol")
                            or (row["symbol"] if row is not None else "")
                            or ""
                        ).strip()
                        or None,
                        "volume": _d(payload.get("volume")) if payload.get("volume") is not None else None,
                        "price": _d(payload.get("price")) if payload.get("price") is not None else None,
                        "profit": _d(payload.get("profit")),
                        "commission": _d(payload.get("commission")),
                        "swap": _d(payload.get("swap")),
                        "occurred_at": occurred_at,
                        "broker_time": str(payload.get("brokerTime") or "").strip() or None,
                        "raw_payload": json.dumps(payload, separators=(",", ":"), default=str),
                    },
                ).first()
                if inserted is not None:
                    added += 1
            session.commit()
        return added

    def _repair_missing_deal_links(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        mapped_rows: list[Any],
    ) -> int:
        """Enrich only missing local links for broker deals we can prove are ours."""
        by_broker_position = _unique_position_index(mapped_rows, "broker_position_id")
        by_broker_order = _unique_position_index(mapped_rows, "broker_order_id")
        by_broker_client = _unique_position_index(mapped_rows, "broker_client_id")
        repaired = 0
        with self._session_factory() as session:
            deals = session.execute(
                text(
                    """
                    SELECT id,broker_position_id,broker_order_id,broker_client_id
                    FROM broker_deals
                    WHERE user_id=:user_id
                      AND mt5_account_id=:mt5_account_id
                      AND signal_id IS NULL
                      AND COALESCE(broker_client_id,'') ~ '^SSX?_'
                    ORDER BY occurred_at,id
                    """
                ),
                {"user_id": user_id, "mt5_account_id": mt5_account_id},
            ).mappings().all()
            for deal in deals:
                row = _resolve_mapped_position(
                    broker_position_id=(
                        str(deal["broker_position_id"]) if deal["broker_position_id"] else None
                    ),
                    broker_order_id=(
                        str(deal["broker_order_id"]) if deal["broker_order_id"] else None
                    ),
                    broker_client_id=(
                        str(deal["broker_client_id"]) if deal["broker_client_id"] else None
                    ),
                    by_broker_position=by_broker_position,
                    by_broker_order=by_broker_order,
                    by_broker_client=by_broker_client,
                )
                if row is None:
                    continue
                trader = trader_stream_for(
                    str(row["source_alias"] or ""),
                    str(row["original_text"] or ""),
                )
                updated = session.execute(
                    text(
                        """
                        UPDATE broker_deals
                        SET position_id=COALESCE(position_id,:position_id),
                            signal_id=:signal_id,
                            source_id=COALESCE(source_id,:source_id),
                            trader_stream=COALESCE(trader_stream,:trader_stream)
                        WHERE id=:deal_id AND signal_id IS NULL
                        RETURNING id
                        """
                    ),
                    {
                        "deal_id": deal["id"],
                        "position_id": row["id"],
                        "signal_id": row["signal_id"],
                        "source_id": row["source_id"],
                        "trader_stream": trader,
                    },
                ).scalar_one_or_none()
                if updated is not None:
                    repaired += 1
            session.commit()
        return repaired

    async def sync_user(self, user_id: UUID) -> Day33SyncResult:
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

        start_time, full_backfill = self._history_start(user_id, account["id"])
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

        added = self._store_account_deals(
            user_id=user_id,
            mt5_account_id=account["id"],
            payloads=payloads,
        )
        if full_backfill:
            self._mark_full_backfill(
                user_id=user_id,
                mt5_account_id=account["id"],
                start_time=start_time,
                end_time=captured_at,
                deal_count=len(payloads),
            )

        mapped_positions = self._mapped_positions(user_id)
        self._repair_missing_deal_links(
            user_id=user_id,
            mt5_account_id=account["id"],
            mapped_rows=mapped_positions,
        )
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

    def _timeline_rows(self, user_id: UUID) -> list[Any]:
        rows = [dict(row) for row in super()._timeline_rows(user_id)]
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
        for item in rows:
            if str(item.get("status_override") or "") == "skipped":
                continue
            repair = repairs.get(item.get("signal_id"))
            if repair is None:
                continue
            if repair["realised_pnl"] is not None:
                item["cash_pnl"] = repair["realised_pnl"]
            if (
                int(repair["known_legs"] or 0) > 0
                and int(repair["missing_pip_legs"] or 0) == 0
            ):
                item["net_pips"] = repair["repaired_pips"]
        return rows
