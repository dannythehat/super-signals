"""Day 33 hardening for incremental, fail-closed broker-history synchronisation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.performance_ledger_day33 import (
    Day33LedgerError,
    Day33PerformanceLedgerService,
    Day33SyncResult,
    Day33TimelineTrade,
    stable_color_index,
    trader_stream_for,
)


class Day33PerformanceLedgerServiceV2(Day33PerformanceLedgerService):
    """Incremental broker history plus privacy-safe shared current trade state."""

    def ledger_ready(self, user_id: UUID) -> bool:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE p.broker_position_id IS NOT NULL)::int AS mapped_count,
                        COUNT(o.position_id)::int AS outcome_count
                    FROM positions p
                    LEFT JOIN performance_trade_outcomes o ON o.position_id=p.id
                    WHERE p.user_id=:user_id
                    """
                ),
                {"user_id": user_id},
            ).mappings().one()
        mapped = int(row["mapped_count"])
        outcomes = int(row["outcome_count"])
        return mapped == 0 or outcomes >= mapped

    def _positions_needing_history(self, user_id: UUID) -> list[Any]:
        rows = self._mapped_positions(user_id)
        with self._session_factory() as session:
            completed_ids = set(
                session.scalars(
                    text(
                        """
                        SELECT position_id
                        FROM performance_trade_outcomes
                        WHERE user_id=:user_id
                          AND status IN ('won','lost','breakeven')
                          AND broker_deal_count > 0
                        """
                    ),
                    {"user_id": user_id},
                ).all()
            )
        return [
            row
            for row in rows
            if row["broker_position_id"] and row["id"] not in completed_ids
        ]

    def _backfill_account_snapshots_from_audit(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
    ) -> int:
        """Reuse the immutable Day 23 pre-trade account reads as balance evidence.

        Day 26/28 already reads MT5 state before any order is sent. Those append-only
        audit events retain balance/equity/currency, so Day 33 can reproduce return
        percentages without adding a new write or failure mode to the trading engine.
        """
        with self._session_factory() as session:
            result = session.execute(
                text(
                    """
                    INSERT INTO performance_account_snapshots (
                        user_id,mt5_account_id,currency,balance,equity,captured_at
                    )
                    SELECT
                        :user_id,
                        :mt5_account_id,
                        COALESCE(NULLIF(a.payload->>'currency',''),'USD'),
                        (a.payload->>'balance')::numeric,
                        COALESCE(NULLIF(a.payload->>'equity',''),a.payload->>'balance')::numeric,
                        a.created_at
                    FROM audit_events a
                    WHERE a.entity_type='mt5_account'
                      AND a.entity_id=:mt5_account_id
                      AND a.event_type='mt5.day23_live_state_read'
                      AND a.payload ? 'balance'
                      AND COALESCE(a.payload->>'balance','') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                      AND COALESCE(NULLIF(a.payload->>'equity',''),a.payload->>'balance','') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                    ON CONFLICT (mt5_account_id,captured_at) DO NOTHING
                    RETURNING id
                    """
                ),
                {"user_id": user_id, "mt5_account_id": mt5_account_id},
            ).all()
            session.commit()
            return len(result)

    def read_shared_live_board(self) -> list[Any]:
        """Return one provider-hidden row per currently open/pending Signal.

        This is deliberately global rather than user-specific. Multi-user copies
        are deduplicated by canonical signal, so Day 34 can publish one shared
        board without revealing users, account counts, balances or provider names.
        Skipped/blocked signals are intentionally absent from this board.
        """
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        """
                        SELECT
                            s.id AS signal_id,
                            COALESCE(s.symbol,'') AS symbol,
                            COALESCE(s.side,'') AS side,
                            COUNT(DISTINCT p.tp_index) FILTER (WHERE o.status='open')::int AS open_positions,
                            COUNT(DISTINCT p.tp_index) FILTER (WHERE o.status='pending')::int AS pending_positions,
                            COUNT(DISTINCT p.tp_index)::int AS position_count,
                            MIN(o.opened_at) AS opened_at
                        FROM performance_trade_outcomes o
                        JOIN positions p ON p.id=o.position_id
                        JOIN signals s ON s.id=o.signal_id
                        GROUP BY s.id,s.symbol,s.side
                        HAVING BOOL_OR(o.status='open') OR BOOL_OR(o.status='pending')
                        ORDER BY MIN(COALESCE(o.opened_at,o.derived_at)), s.id
                        """
                    )
                ).mappings().all()
            )

    def _timeline_rows(self, user_id: UUID) -> list[Any]:
        """Return executed broker outcomes plus real execution-block evidence.

        A skipped row comes only from a persisted execution-block audit event for
        this user and is suppressed if the same Signal subsequently acquired an
        outcome. It is observability only and never enters the live Telegram board.
        """
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        """
                        WITH executed AS (
                            SELECT
                                s.id AS signal_id,
                                COALESCE(s.symbol,'') AS symbol,
                                COALESCE(s.side,'') AS side,
                                s.source_id,
                                COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label,
                                s.original_text,
                                MIN(o.opened_at) AS opened_at,
                                MAX(o.closed_at) AS closed_at,
                                COUNT(o.position_id)::int AS position_count,
                                COUNT(*) FILTER (WHERE o.status='open')::int AS open_positions,
                                COUNT(*) FILTER (WHERE o.status='pending')::int AS pending_positions,
                                COUNT(*) FILTER (WHERE o.status IN ('won','lost','breakeven','closed_unknown'))::int AS closed_positions,
                                SUM(o.cash_pnl) FILTER (WHERE o.cash_pnl IS NOT NULL) AS cash_pnl,
                                CASE
                                    WHEN COUNT(DISTINCT o.symbol) FILTER (WHERE o.net_pips IS NOT NULL)<=1
                                     AND COUNT(*) FILTER (
                                         WHERE o.status IN ('won','lost','breakeven')
                                           AND o.net_pips IS NULL
                                     )=0
                                    THEN SUM(o.net_pips)
                                    ELSE NULL
                                END AS net_pips,
                                SUM(o.model_500_pnl) FILTER (WHERE o.model_500_pnl IS NOT NULL) AS model_500_pnl,
                                MAX(o.trader_stream) FILTER (WHERE o.trader_stream IS NOT NULL) AS trader_stream,
                                MAX(o.close_reason) FILTER (WHERE o.close_reason IS NOT NULL) AS close_reason,
                                BOOL_OR(o.status='won') AS has_win,
                                BOOL_OR(o.status='lost') AS has_loss,
                                BOOL_OR(o.status='breakeven') AS has_breakeven,
                                BOOL_OR(o.status='closed_unknown') AS has_unknown,
                                NULL::text AS status_override,
                                MAX(COALESCE(o.opened_at,o.closed_at,o.derived_at)) AS sort_at
                            FROM performance_trade_outcomes o
                            JOIN signals s ON s.id=o.signal_id
                            LEFT JOIN sources src ON src.id=s.source_id
                            WHERE o.user_id=:user_id
                            GROUP BY s.id,s.symbol,s.side,s.source_id,src.source_alias,src.chat_title,s.original_text
                        ),
                        skipped AS (
                            SELECT
                                s.id AS signal_id,
                                COALESCE(s.symbol,'') AS symbol,
                                COALESCE(s.side,'') AS side,
                                s.source_id,
                                COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label,
                                s.original_text,
                                a.created_at AS opened_at,
                                a.created_at AS closed_at,
                                0::int AS position_count,
                                0::int AS open_positions,
                                0::int AS pending_positions,
                                0::int AS closed_positions,
                                NULL::numeric AS cash_pnl,
                                NULL::numeric AS net_pips,
                                NULL::numeric AS model_500_pnl,
                                NULL::text AS trader_stream,
                                COALESCE(a.payload->>'error_code','execution_blocked') AS close_reason,
                                false AS has_win,
                                false AS has_loss,
                                false AS has_breakeven,
                                false AS has_unknown,
                                'skipped'::text AS status_override,
                                a.created_at AS sort_at
                            FROM audit_events a
                            JOIN signals s ON s.id=a.entity_id
                            LEFT JOIN sources src ON src.id=s.source_id
                            WHERE a.actor_user_id=:user_id
                              AND a.entity_type='signal'
                              AND a.event_type='mt5.day26_execution_blocked'
                              AND COALESCE((a.payload->>'trade_action_created')::boolean,false)=false
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM performance_trade_outcomes o
                                  WHERE o.user_id=:user_id AND o.signal_id=s.id
                              )
                        )
                        SELECT * FROM executed
                        UNION ALL
                        SELECT * FROM skipped
                        ORDER BY sort_at DESC, signal_id
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().all()
            )

    @staticmethod
    def _timeline_trade(row: Any, *, provider_visible: bool) -> Day33TimelineTrade:
        if str(row.get("status_override") or "") != "skipped":
            return Day33PerformanceLedgerService._timeline_trade(
                row,
                provider_visible=provider_visible,
            )

        source_id = row["source_id"]
        trader = trader_stream_for(
            str(row["source_label"] or ""),
            str(row["original_text"] or ""),
        )
        return Day33TimelineTrade(
            signal_id=row["signal_id"],
            symbol=str(row["symbol"] or ""),
            side=str(row["side"] or ""),
            status="skipped",
            status_label="Skipped",
            status_color="amber",
            source_label=(str(row["source_label"]) if provider_visible else None),
            trader_stream=(trader if provider_visible else None),
            source_color_index=(
                stable_color_index(source_id, trader)
                if provider_visible and source_id
                else None
            ),
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            position_count=0,
            open_positions=0,
            pending_positions=0,
            closed_positions=0,
            cash_pnl=None,
            net_pips=None,
            model_500_pnl=None,
            close_reason=(str(row["close_reason"]) if row["close_reason"] else None),
        )

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

        # Import the pre-trade Day 23 reads before deriving returns. This does not
        # contact the broker and does not change any trading state.
        self._backfill_account_snapshots_from_audit(
            user_id=user_id,
            mt5_account_id=account["id"],
        )
        self._store_snapshot(
            user_id=user_id,
            mt5_account_id=account["id"],
            payload=account_payload,
            captured_at=datetime.now(UTC),
        )

        positions = self._positions_needing_history(user_id)
        deals_added = 0
        for row in positions:
            try:
                payloads = await self._gateway.read_deals_by_position(
                    token=token,
                    account_id=account_id,
                    region=region,
                    position_id=str(row["broker_position_id"]),
                )
            except MetaApiGatewayError as exc:
                if exc.code == "metaapi_terminal_data_unavailable":
                    # Old/synthetic acceptance IDs may genuinely not exist at the
                    # broker. Keep them unresolved; never manufacture P/L.
                    payloads = []
                else:
                    raise Day33LedgerError(exc.code, retryable=exc.retryable) from exc
            deals_added += self._store_deals(
                user_id=user_id,
                mt5_account_id=account["id"],
                position_row=row,
                payloads=payloads,
            )

        outcomes = self.rebuild_outcomes(user_id)
        summaries = self.rebuild_summaries(user_id)
        return Day33SyncResult(
            user_id=user_id,
            broker_deals_added=deals_added,
            mapped_positions_checked=len(positions),
            outcomes_rebuilt=outcomes,
            summaries_rebuilt=summaries,
            broker_trade_action_created=False,
        )
