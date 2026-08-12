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

    def read_shared_live_board(self) -> list[Any]:
        """Return one provider-hidden row per currently open/pending Signal.

        This is deliberately global rather than user-specific. Multi-user copies
        are deduplicated by canonical signal, so Day 34 can publish one shared
        board without revealing users, account counts, balances or provider names.
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
