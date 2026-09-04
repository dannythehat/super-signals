"""One-off all-user clean restart for 4 September 2026 at 11:00 Europe/Sofia.

Product intent:
- every account connected at deployment gets a zero-P/L reporting restart at 11:00 Sofia;
- all legacy open/pending mapped exposure created before the boundary is closed/cancelled;
- new entries are held until the boundary, so nothing can leak across the restart;
- immutable broker/audit evidence is preserved;
- user-facing trade history hides the reset-window trades while older historical days stay visible.

The module is idempotent and safe to re-run after the boundary: it only targets positions
opened before the fixed cutoff and only holds execution while current time is before it.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import get_session_factory
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

TIMEZONE = "Europe/Sofia"
REPORTING_DATE = date(2026, 9, 4)
# 11:00 Europe/Sofia on 4 Sep 2026 = 08:00 UTC (EEST, UTC+3).
RESTART_CUTOFF = datetime(2026, 9, 4, 8, 0, 0, tzinfo=UTC)
REASON = (
    "4 September 2026 clean restart boundary at 11:00 Europe/Sofia. "
    "All trading activity opened before the boundary is excluded from user-facing "
    "performance and trade history. Immutable broker evidence is preserved. "
    "Only trades opened at or after 11:00 Europe/Sofia count forward."
)


def _broker_keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def _incident_key(user_id: UUID) -> str:
    return f"restart-2026-09-04-1100-{str(user_id).replace('-', '')[:32]}"


def install_execution_hold() -> None:
    """Fail closed for new entries until exactly 11:00 Sofia."""
    from app.mt5_execution_day26 import Day26ExecutionError, Day26Mt5ExecutionService

    current = Day26Mt5ExecutionService.execute_owner_demo_signal
    if getattr(current, "_ss_restart_1100_guard", False):
        return

    async def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        if datetime.now(UTC) < RESTART_CUTOFF:
            raise Day26ExecutionError("scheduled_clean_restart_hold_until_1100_sofia")
        return await current(self, *args, **kwargs)

    guarded._ss_restart_1100_guard = True  # type: ignore[attr-defined]
    Day26Mt5ExecutionService.execute_owner_demo_signal = guarded
    print("SUPER_SIGNALS_RESTART_1100_EXECUTION_HOLD=ACTIVE", flush=True)


def install_timeline_filter() -> None:
    """Hide reset-window trades from all authenticated/public timeline tables."""
    from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2

    current = Day33PerformanceLedgerServiceV2._timeline_rows
    if getattr(current, "_ss_restart_1100_filter", False):
        return

    def filtered(self: Any, user_id: UUID) -> list[Any]:
        rows = [dict(row) for row in current(self, user_id)]
        with self._session_factory() as session:
            override = session.execute(
                text(
                    """
                    SELECT cutoff_at,
                           (reporting_date::timestamp AT TIME ZONE timezone) AS day_start
                    FROM performance_reporting_overrides
                    WHERE user_id=:user_id
                      AND incident_key LIKE 'restart-2026-09-04-1100-%'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if override is None:
            return rows

        cutoff = override["cutoff_at"]
        day_start = override["day_start"]
        visible: list[Any] = []
        for row in rows:
            opened_at = row.get("opened_at")
            closed_at = row.get("closed_at")
            open_positions = int(row.get("open_positions") or 0)
            pending_positions = int(row.get("pending_positions") or 0)
            if (
                opened_at is not None
                and opened_at < cutoff
                and (
                    open_positions > 0
                    or pending_positions > 0
                    or (closed_at is not None and closed_at >= day_start)
                )
            ):
                continue
            visible.append(row)
        return visible

    filtered._ss_restart_1100_filter = True  # type: ignore[attr-defined]
    Day33PerformanceLedgerServiceV2._timeline_rows = filtered
    print("SUPER_SIGNALS_RESTART_1100_TIMELINE_FILTER=ACTIVE", flush=True)


def _upsert_overrides(user_ids: list[UUID]) -> None:
    session_factory = get_session_factory()
    with session_factory() as session:
        for user_id in user_ids:
            session.execute(
                text(
                    """
                    INSERT INTO performance_reporting_overrides
                        (user_id, reporting_date, timezone, realised_cash_pnl,
                         reason, incident_key, cutoff_at, created_at, updated_at)
                    VALUES
                        (:user_id, :reporting_date, :timezone, 0,
                         :reason, :incident_key, :cutoff, now(), now())
                    ON CONFLICT (user_id, reporting_date) DO UPDATE
                    SET timezone=EXCLUDED.timezone,
                        realised_cash_pnl=0,
                        reason=EXCLUDED.reason,
                        incident_key=EXCLUDED.incident_key,
                        cutoff_at=EXCLUDED.cutoff_at,
                        updated_at=now()
                    """
                ),
                {
                    "user_id": user_id,
                    "reporting_date": REPORTING_DATE,
                    "timezone": TIMEZONE,
                    "reason": REASON,
                    "incident_key": _incident_key(user_id),
                    "cutoff": RESTART_CUTOFF,
                },
            )
        session.commit()

        for user_id in user_ids:
            override_id = session.execute(
                text(
                    """
                    SELECT id FROM performance_reporting_overrides
                    WHERE user_id=:user_id AND reporting_date=:reporting_date
                    """
                ),
                {"user_id": user_id, "reporting_date": REPORTING_DATE},
            ).scalar_one()
            exists = session.execute(
                text(
                    """
                    SELECT 1 FROM audit_events
                    WHERE actor_user_id=:user_id
                      AND event_type='performance.clean_restart_1100_created'
                      AND entity_id=:entity_id
                    LIMIT 1
                    """
                ),
                {"user_id": user_id, "entity_id": override_id},
            ).scalar_one_or_none()
            if exists is None:
                session.execute(
                    text(
                        """
                        INSERT INTO audit_events
                            (actor_user_id,event_type,entity_type,entity_id,payload)
                        VALUES
                            (:user_id,'performance.clean_restart_1100_created',
                             'performance_reporting_override',:entity_id,
                             jsonb_build_object(
                                'reporting_date', :reporting_date,
                                'timezone', :timezone,
                                'cutoff_at', :cutoff,
                                'realised_cash_pnl', 0,
                                'raw_broker_evidence_preserved', true,
                                'all_pre_boundary_trades_excluded', true
                             ))
                        """
                    ),
                    {
                        "user_id": user_id,
                        "entity_id": override_id,
                        "reporting_date": REPORTING_DATE,
                        "timezone": TIMEZONE,
                        "cutoff": RESTART_CUTOFF,
                    },
                )
        session.commit()


async def run_connected_account_restart() -> None:
    """Close/cancel all legacy exposure for every currently connected MT5 account."""
    session_factory = get_session_factory()
    with session_factory() as session:
        accounts = session.execute(
            text(
                """
                SELECT DISTINCT ON (m.owner_user_id)
                       m.owner_user_id AS user_id,
                       m.metaapi_account_id,
                       m.metaapi_token_ciphertext
                FROM mt5_accounts m
                JOIN users u ON u.id=m.owner_user_id
                WHERE m.status='connected'
                  AND u.status NOT IN ('revoked','suspended')
                ORDER BY m.owner_user_id, m.created_at DESC
                """
            )
        ).mappings().all()

    user_ids = [UUID(str(row["user_id"])) for row in accounts]
    _upsert_overrides(user_ids)
    if not accounts:
        print("SUPER_SIGNALS_RESTART_1100=NO_CONNECTED_ACCOUNTS", flush=True)
        return

    keys = _broker_keys()
    if not keys:
        print("SUPER_SIGNALS_RESTART_1100=FAIL_NO_BROKER_KEYS", flush=True)
        return
    cipher = MetaApiTokenCipher(keys)
    read = MetaApiReadGateway()
    trade = MetaApiTradeGateway()

    total_rows = total_closed = total_cancelled = total_reconciled = 0
    failures: list[str] = []

    for account in accounts:
        user_id = UUID(str(account["user_id"]))
        with session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT p.id,p.status,p.broker_position_id,p.broker_order_id,p.broker_client_id
                    FROM positions p
                    WHERE p.user_id=:user_id
                      AND p.status IN ('open','pending')
                      AND COALESCE(p.opened_at,p.created_at)<:cutoff
                    ORDER BY COALESCE(p.opened_at,p.created_at),p.id
                    """
                ),
                {"user_id": user_id, "cutoff": RESTART_CUTOFF},
            ).mappings().all()
        total_rows += len(rows)
        if not rows:
            continue

        try:
            token = cipher.decrypt(bytes(account["metaapi_token_ciphertext"])).strip()
        except BrokerCredentialDecryptionError:
            failures.append(f"{user_id}:decrypt")
            continue

        try:
            region = await read.resolve_account_region(
                token=token,
                account_id=str(account["metaapi_account_id"]),
            )
            broker_positions = await read.read_positions(
                token=token,
                account_id=str(account["metaapi_account_id"]),
                region=region,
            )
            broker_orders = await read.read_orders(
                token=token,
                account_id=str(account["metaapi_account_id"]),
                region=region,
            )
        except MetaApiGatewayError as exc:
            failures.append(f"{user_id}:read:{exc.code}")
            continue

        by_id = {str(x.get("id") or "").strip(): x for x in broker_positions if str(x.get("id") or "").strip()}
        by_client = {str(x.get("clientId") or "").strip(): x for x in broker_positions if str(x.get("clientId") or "").strip()}
        by_order = {str(x.get("orderId") or "").strip(): x for x in broker_positions if str(x.get("orderId") or "").strip()}
        active_orders = {str(x.get("id") or "").strip() for x in broker_orders if str(x.get("id") or "").strip()}

        for row in rows:
            position_id = str(row["broker_position_id"] or "").strip()
            order_id = str(row["broker_order_id"] or "").strip()
            client_id = str(row["broker_client_id"] or "").strip()
            mapped = by_id.get(position_id) or by_client.get(client_id) or by_order.get(order_id)
            now = datetime.now(UTC)
            try:
                if mapped is not None:
                    broker_position_id = str(mapped.get("id") or "").strip()
                    await trade.close_position(
                        token=token,
                        account_id=str(account["metaapi_account_id"]),
                        region=region,
                        position_id=broker_position_id,
                    )
                    with session_factory() as session:
                        session.execute(
                            text(
                                """
                                UPDATE positions
                                SET status='closed',closed_at=COALESCE(closed_at,:now),
                                    close_reason='clean_restart_1100_sofia',updated_at=:now
                                WHERE id=:id AND user_id=:user_id AND status IN ('open','pending')
                                """
                            ),
                            {"id": row["id"], "user_id": user_id, "now": now},
                        )
                        session.commit()
                    total_closed += 1
                elif order_id and order_id in active_orders:
                    await trade.cancel_order(
                        token=token,
                        account_id=str(account["metaapi_account_id"]),
                        region=region,
                        order_id=order_id,
                    )
                    with session_factory() as session:
                        session.execute(
                            text(
                                """
                                UPDATE positions
                                SET status='skipped',closed_at=COALESCE(closed_at,:now),
                                    close_reason='clean_restart_1100_sofia',updated_at=:now
                                WHERE id=:id AND user_id=:user_id AND status='pending'
                                """
                            ),
                            {"id": row["id"], "user_id": user_id, "now": now},
                        )
                        session.commit()
                    total_cancelled += 1
                else:
                    with session_factory() as session:
                        terminal = 'skipped' if str(row["status"]) == 'pending' else 'closed'
                        session.execute(
                            text(
                                """
                                UPDATE positions
                                SET status=:terminal,closed_at=COALESCE(closed_at,:now),
                                    close_reason='clean_restart_1100_reconciled_no_broker_exposure',
                                    updated_at=:now
                                WHERE id=:id AND user_id=:user_id AND status IN ('open','pending')
                                """
                            ),
                            {"terminal": terminal, "id": row["id"], "user_id": user_id, "now": now},
                        )
                        session.commit()
                    total_reconciled += 1
            except (MetaApiGatewayError, RuntimeError) as exc:
                failures.append(f"{user_id}:{row['id']}:{getattr(exc, 'code', type(exc).__name__)}")

        await asyncio.sleep(0.5)
        try:
            after_positions = await read.read_positions(
                token=token,
                account_id=str(account["metaapi_account_id"]),
                region=region,
            )
            after_orders = await read.read_orders(
                token=token,
                account_id=str(account["metaapi_account_id"]),
                region=region,
            )
        except MetaApiGatewayError as exc:
            failures.append(f"{user_id}:verify:{exc.code}")
            continue

        target_position_ids = {str(row["broker_position_id"] or "").strip() for row in rows}
        target_order_ids = {str(row["broker_order_id"] or "").strip() for row in rows}
        target_client_ids = {str(row["broker_client_id"] or "").strip() for row in rows}
        target_position_ids.discard("")
        target_order_ids.discard("")
        target_client_ids.discard("")
        remaining_positions = [
            x for x in after_positions
            if str(x.get("id") or "").strip() in target_position_ids
            or str(x.get("orderId") or "").strip() in target_order_ids
            or str(x.get("clientId") or "").strip() in target_client_ids
        ]
        remaining_orders = [
            x for x in after_orders
            if str(x.get("id") or "").strip() in target_order_ids
            or str(x.get("clientId") or "").strip() in target_client_ids
        ]
        if remaining_positions or remaining_orders:
            failures.append(
                f"{user_id}:remaining_positions={len(remaining_positions)}:remaining_orders={len(remaining_orders)}"
            )

    status = "PASS" if not failures else "FAIL"
    print(
        f"SUPER_SIGNALS_RESTART_1100={status} connected={len(accounts)} rows={total_rows} "
        f"closed={total_closed} cancelled={total_cancelled} reconciled={total_reconciled} "
        f"failures={len(failures)} cutoff={RESTART_CUTOFF.isoformat()}",
        flush=True,
    )
    for failure in failures[:20]:
        print(f"SUPER_SIGNALS_RESTART_1100_FAILURE={failure}", flush=True)


__all__ = [
    "RESTART_CUTOFF",
    "install_execution_hold",
    "install_timeline_filter",
    "run_connected_account_restart",
]
