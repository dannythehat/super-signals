"""Canonical broker-account performance ledger.

The dashboard and settlement watcher use one broker-truth model:
* account snapshots come from MetaAPI account information;
* the complete MT5 deal stream is synchronised, not only already-known positions;
* broker_deals is append-only and duplicate broker deal IDs are never rewritten;
* first sync backfills the available Super Signals evidence period, later syncs overlap
  five minutes from the newest stored deal;
* completed XAUUSD pips are derived from broker entry/exit prices when the stored pips
  field is absent;
* account balance movement can be reconciled to stored broker cash movements.
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger(__name__)

_CHECKPOINT_EVENT = "mt5.performance_full_history_backfilled"
_HISTORY_OVERLAP = timedelta(minutes=5)
_PARTIAL_SYNC_TRUNCATED_EVENT = "mt5.performance_deal_sync_partial_truncated"
_PARTIAL_SYNC_RESOLVED_EVENT = "mt5.performance_deal_sync_partial_resolved"

# One-time incident repair: balance-snapshot reconciliation on 29 Sep 2026 proved these
# two fixed windows lost real broker deals to the (now fixed) silent
# metaapi_terminal_data_unavailable truncation - $169.83 on the Friday, $55.67 on the
# Monday, confirmed by comparing consecutive account balance snapshots (which come
# straight from the broker) against what broker_deals had stored for the same window.
# Both bounds are fixed incident timestamps, never a moving "now"-relative window - see
# _cleanup_accidental_historical_settlement_replay_safely for why that matters.
_KNOWN_DEAL_GAP_EVENT = "mt5.performance_known_deal_gap_repaired"
_KNOWN_DEAL_GAP_WINDOWS: tuple[tuple[str, datetime, datetime], ...] = (
    (
        "2026-09-25_fri_terminal_unavailable_gap",
        datetime(2026, 9, 24, 18, 0, tzinfo=UTC),
        datetime(2026, 9, 25, 18, 0, tzinfo=UTC),
    ),
    (
        "2026-09-28_mon_terminal_unavailable_gap",
        datetime(2026, 9, 27, 18, 0, tzinfo=UTC),
        datetime(2026, 9, 28, 18, 0, tzinfo=UTC),
    ),
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
                start_time, full_backfill = _as_utc(anchor) - _HISTORY_OVERLAP, False
            else:
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

                candidates = [
                    value for value in (first_snapshot, first_position) if value is not None
                ]
                if not candidates:
                    start_time, full_backfill = datetime.now(UTC) - timedelta(days=7), True
                else:
                    start_time, full_backfill = (
                        min(_as_utc(value) for value in candidates) - timedelta(seconds=2),
                        True,
                    )

        # A prior sync may have had its broker deal-history fetch cut short mid-page by
        # a transient metaapi_terminal_data_unavailable error (see sync_user). The stored
        # watermark above only reflects whatever partial page was already fetched before
        # that happened, so relying on it alone can silently and permanently skip the
        # deals between where the partial page ended and where the fetch was cut off.
        # Re-widen the start back to that earlier sync's original request until a sync
        # completes cleanly across it.
        outstanding = self._outstanding_partial_sync_start(user_id, mt5_account_id)
        if outstanding is not None and outstanding < start_time:
            start_time = outstanding
        return start_time, full_backfill

    def _outstanding_partial_sync_start(
        self,
        user_id: UUID,
        mt5_account_id: UUID,
    ) -> datetime | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT event_type, payload
                    FROM audit_events
                    WHERE actor_user_id=:user_id
                      AND entity_type='mt5_account'
                      AND entity_id=:account_id
                      AND event_type IN (:truncated_event, :resolved_event)
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "user_id": user_id,
                    "account_id": mt5_account_id,
                    "truncated_event": _PARTIAL_SYNC_TRUNCATED_EVENT,
                    "resolved_event": _PARTIAL_SYNC_RESOLVED_EVENT,
                },
            ).mappings().first()
        if row is None or row["event_type"] != _PARTIAL_SYNC_TRUNCATED_EVENT:
            return None
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        raw = payload.get("requested_start_time")
        if not raw:
            return None
        try:
            return _as_utc(datetime.fromisoformat(raw))
        except ValueError:
            return None

    def _record_partial_sync_truncation(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        requested_start_time: datetime,
        requested_end_time: datetime,
        payloads_fetched: int,
        offset_reached: int,
        error_code: str,
    ) -> None:
        payload = json.dumps(
            {
                "requested_start_time": requested_start_time.isoformat(),
                "requested_end_time": requested_end_time.isoformat(),
                "payloads_fetched": payloads_fetched,
                "offset_reached": offset_reached,
                "error_code": error_code,
            },
            separators=(",", ":"),
        )
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        actor_user_id,event_type,entity_type,entity_id,payload
                    ) VALUES (
                        :user_id,:event_type,'mt5_account',:account_id,CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "user_id": user_id,
                    "event_type": _PARTIAL_SYNC_TRUNCATED_EVENT,
                    "account_id": mt5_account_id,
                    "payload": payload,
                },
            )
            session.commit()

    def _record_partial_sync_resolved(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        actor_user_id,event_type,entity_type,entity_id,payload
                    ) VALUES (
                        :user_id,:event_type,'mt5_account',:account_id,'{}'::jsonb
                    )
                    """
                ),
                {
                    "user_id": user_id,
                    "event_type": _PARTIAL_SYNC_RESOLVED_EVENT,
                    "account_id": mt5_account_id,
                },
            )
            session.commit()

    def _repaired_deal_gap_windows(
        self,
        user_id: UUID,
        mt5_account_id: UUID,
    ) -> set[str]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT payload->>'window_id' AS window_id
                    FROM audit_events
                    WHERE actor_user_id=:user_id
                      AND entity_type='mt5_account'
                      AND entity_id=:account_id
                      AND event_type=:event_type
                    """
                ),
                {
                    "user_id": user_id,
                    "account_id": mt5_account_id,
                    "event_type": _KNOWN_DEAL_GAP_EVENT,
                },
            ).scalars().all()
        return {row for row in rows if row}

    def _mark_deal_gap_repaired(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        window_id: str,
        deals_added: int,
    ) -> None:
        payload = json.dumps(
            {"window_id": window_id, "deals_added": deals_added},
            separators=(",", ":"),
        )
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        actor_user_id,event_type,entity_type,entity_id,payload
                    ) VALUES (
                        :user_id,:event_type,'mt5_account',:account_id,CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "user_id": user_id,
                    "event_type": _KNOWN_DEAL_GAP_EVENT,
                    "account_id": mt5_account_id,
                    "payload": payload,
                },
            )
            session.commit()

    async def _repair_known_deal_gaps_safely(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        token: str,
        account_id: str,
        region: str,
    ) -> int:
        """Re-fetch the two fixed incident windows (see _KNOWN_DEAL_GAP_WINDOWS) once
        per account. Safe to call on every sync: broker_deals' own ON CONFLICT DO
        NOTHING makes a repeat fetch a no-op, and each window is additionally marked
        done so it stops calling the broker at all once it has cleanly repaired once.
        """
        already_repaired = self._repaired_deal_gap_windows(user_id, mt5_account_id)
        total_added = 0
        for window_id, window_start, window_end in _KNOWN_DEAL_GAP_WINDOWS:
            if window_id in already_repaired:
                continue
            payloads: list[dict[str, object]] = []
            offset = 0
            page_size = 1000
            clean = True
            try:
                while True:
                    page = await self._gateway.read_deals_by_time_range(
                        token=token,
                        account_id=account_id,
                        region=region,
                        start_time=window_start,
                        end_time=window_end,
                        offset=offset,
                        limit=page_size,
                    )
                    payloads.extend(page)
                    if len(page) < page_size:
                        break
                    offset += len(page)
                    if offset > 100_000:
                        clean = False
                        break
            except MetaApiGatewayError:
                clean = False
            added = self._store_account_deals(
                user_id=user_id,
                mt5_account_id=mt5_account_id,
                payloads=payloads,
            )
            total_added += added
            if clean:
                self._mark_deal_gap_repaired(
                    user_id=user_id,
                    mt5_account_id=mt5_account_id,
                    window_id=window_id,
                    deals_added=added,
                )
        return total_added

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

    async def sync_user(
        self,
        user_id: UUID,
        *,
        max_history_positions: int | None = None,
        rebuild_summaries: bool = True,
    ) -> Day33SyncResult:
        # The live settlement watcher needs a bounded, current-position path. Use
        # the Day 33 v2 per-position sync for that mode; retain canonical account-wide
        # history sync for normal dashboard/backfill calls.
        if max_history_positions is not None or not rebuild_summaries:
            return await super().sync_user(
                user_id,
                max_history_positions=max_history_positions,
                rebuild_summaries=rebuild_summaries,
            )

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
        truncation_error_code: str | None = None
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
            # A terminal blip mid-pagination still leaves whatever pages already came
            # back in `payloads`. Storing them is correct (real deals, don't discard
            # them) but accepting this as a clean sync would hide that everything from
            # here to `captured_at` was never actually fetched. Record the truncation so
            # `_history_start` re-widens the next attempt back over the gap, instead of
            # trusting the partial watermark this run is about to leave behind.
            truncation_error_code = exc.code

        added = self._store_account_deals(
            user_id=user_id,
            mt5_account_id=account["id"],
            payloads=payloads,
        )
        if truncation_error_code is not None and payloads:
            self._record_partial_sync_truncation(
                user_id=user_id,
                mt5_account_id=account["id"],
                requested_start_time=start_time,
                requested_end_time=captured_at,
                payloads_fetched=len(payloads),
                offset_reached=offset,
                error_code=truncation_error_code,
            )
        elif truncation_error_code is None:
            if self._outstanding_partial_sync_start(user_id, account["id"]) is not None:
                self._record_partial_sync_resolved(user_id=user_id, mt5_account_id=account["id"])
            if full_backfill:
                self._mark_full_backfill(
                    user_id=user_id,
                    mt5_account_id=account["id"],
                    start_time=start_time,
                    end_time=captured_at,
                    deal_count=len(payloads),
                )

        try:
            await self._repair_known_deal_gaps_safely(
                user_id=user_id,
                mt5_account_id=account["id"],
                token=token,
                account_id=account_id,
                region=region,
            )
        except Exception:
            logger.exception("Known deal-gap repair failed safely; normal sync is unaffected")

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

    def read_account_reconciliation(
        self,
        user_id: UUID,
        *,
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, Decimal | bool | None]:
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


__all__ = ["CanonicalPerformanceLedgerService"]
