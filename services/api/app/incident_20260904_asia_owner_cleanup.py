"""Owner-account correction for the 2026-09-04 TIG Asia Trades incident.

This one-shot repair is intentionally narrow:
- resolve the actual Owner by RBAC role, never by newest MT5 account;
- preserve the newest accepted Asia XAUUSD signal;
- close/cancel every older mapped Asia exposure by exact broker identifiers;
- verify broker truth after mutations;
- quarantine pre-cutoff Asia P/L from the Owner's official reporting while preserving
  all other providers and immutable broker evidence;
- remove the short-lived override accidentally written to a non-owner demo account by
  the first emergency attempt.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.db import get_session_factory
from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

INCIDENT_KEY = "incident-2026-09-04-asia-management-reader"
CLEANUP_REASON = "incident_asia_stale_cleanup_20260904"
TIMEZONE = "Europe/Sofia"


def _broker_keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    return tuple(value.strip() for value in raw.split(",") if value.strip())


async def run_owner_asia_cleanup() -> None:
    session_factory = get_session_factory()

    with session_factory() as session:
        owner = session.execute(
            text(
                """
                SELECT u.id
                FROM users u
                JOIN user_roles ur ON ur.user_id=u.id
                JOIN roles r ON r.id=ur.role_id
                WHERE r.name='owner'
                ORDER BY u.created_at
                LIMIT 1
                """
            )
        ).mappings().first()
        if owner is None:
            print("SUPER_SIGNALS_ASIA_OWNER_CLEANUP=FAIL_NO_OWNER", flush=True)
            return
        owner_user_id = owner["id"]

        # Remove only the accidental incident override if it was written to a user
        # other than the actual Owner. No broker evidence or outcomes are touched.
        removed_wrong_override = session.execute(
            text(
                """
                DELETE FROM performance_reporting_overrides
                WHERE incident_key=:incident_key AND user_id<>:owner_user_id
                RETURNING id
                """
            ),
            {"incident_key": INCIDENT_KEY, "owner_user_id": owner_user_id},
        ).all()
        if removed_wrong_override:
            session.commit()
            print(
                f"SUPER_SIGNALS_ASIA_WRONG_OVERRIDE_REMOVED={len(removed_wrong_override)}",
                flush=True,
            )

        already_done = session.execute(
            text(
                """
                SELECT 1 FROM performance_reporting_overrides
                WHERE incident_key=:incident_key AND user_id=:owner_user_id
                LIMIT 1
                """
            ),
            {"incident_key": INCIDENT_KEY, "owner_user_id": owner_user_id},
        ).scalar_one_or_none()
        if already_done is not None:
            print("SUPER_SIGNALS_ASIA_OWNER_CLEANUP=ALREADY_DONE", flush=True)
            return

        account = session.execute(
            text(
                """
                SELECT metaapi_account_id, metaapi_token_ciphertext
                FROM mt5_accounts
                WHERE owner_user_id=:owner_user_id
                  AND status='connected'
                  AND account_environment='demo'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"owner_user_id": owner_user_id},
        ).mappings().first()
        source = session.execute(
            text(
                """
                SELECT id
                FROM sources
                WHERE status<>'revoked'
                  AND (source_alias ILIKE '%Asia Trades%' OR chat_title ILIKE '%Asia Trades%')
                ORDER BY updated_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()

    if account is None or source is None:
        print("SUPER_SIGNALS_ASIA_OWNER_CLEANUP=FAIL_ACCOUNT_OR_SOURCE", flush=True)
        return

    source_id = source["id"]
    with session_factory() as session:
        latest = session.execute(
            text(
                """
                SELECT id, provider_message_id, source_revision_index, source_posted_at
                FROM signals
                WHERE source_id=:source_id
                  AND UPPER(symbol)='XAUUSD'
                  AND parser_status='accepted'
                ORDER BY provider_message_id DESC NULLS LAST,
                         source_revision_index DESC,
                         source_posted_at DESC
                LIMIT 1
                """
            ),
            {"source_id": source_id},
        ).mappings().first()
        if latest is None:
            print("SUPER_SIGNALS_ASIA_OWNER_CLEANUP=FAIL_NO_LATEST_SIGNAL", flush=True)
            return

        rows = session.execute(
            text(
                """
                SELECT p.id, p.signal_id, p.status, p.broker_position_id,
                       p.broker_order_id, p.broker_client_id,
                       s.provider_message_id, s.source_posted_at
                FROM positions p
                JOIN signals s ON s.id=p.signal_id
                WHERE p.user_id=:user_id
                  AND s.source_id=:source_id
                  AND p.status IN ('open','pending')
                  AND s.id<>:latest_signal_id
                  AND (
                    (s.provider_message_id IS NOT NULL
                     AND :latest_message_id IS NOT NULL
                     AND s.provider_message_id < :latest_message_id)
                    OR
                    ((s.provider_message_id IS NULL OR :latest_message_id IS NULL)
                     AND s.source_posted_at < :latest_posted_at)
                  )
                ORDER BY s.source_posted_at, p.created_at, p.id
                """
            ),
            {
                "user_id": owner_user_id,
                "source_id": source_id,
                "latest_signal_id": latest["id"],
                "latest_message_id": latest["provider_message_id"],
                "latest_posted_at": latest["source_posted_at"],
            },
        ).mappings().all()

    keys = _broker_keys()
    if not keys:
        print("SUPER_SIGNALS_ASIA_OWNER_CLEANUP=FAIL_NO_BROKER_KEYS", flush=True)
        return
    cipher = MetaApiTokenCipher(keys)
    try:
        token = cipher.decrypt(bytes(account["metaapi_token_ciphertext"])).strip()
    except BrokerCredentialDecryptionError:
        print("SUPER_SIGNALS_ASIA_OWNER_CLEANUP=FAIL_DECRYPT", flush=True)
        return

    read = MetaApiReadGateway()
    trade = MetaApiTradeGateway()
    try:
        region = await read.resolve_account_region(
            token=token,
            account_id=str(account["metaapi_account_id"]),
        )
        before_positions = await read.read_positions(
            token=token,
            account_id=str(account["metaapi_account_id"]),
            region=region,
        )
        before_orders = await read.read_orders(
            token=token,
            account_id=str(account["metaapi_account_id"]),
            region=region,
        )
    except MetaApiGatewayError as exc:
        print(f"SUPER_SIGNALS_ASIA_OWNER_CLEANUP=FAIL_BROKER_READ:{exc.code}", flush=True)
        return

    by_id = {
        str(item.get("id") or "").strip(): item
        for item in before_positions
        if str(item.get("id") or "").strip()
    }
    by_client = {
        str(item.get("clientId") or "").strip(): item
        for item in before_positions
        if str(item.get("clientId") or "").strip()
    }
    by_order = {
        str(item.get("orderId") or "").strip(): item
        for item in before_positions
        if str(item.get("orderId") or "").strip()
    }
    active_orders = {
        str(item.get("id") or "").strip()
        for item in before_orders
        if str(item.get("id") or "").strip()
    }

    closed = 0
    cancelled = 0
    reconciled = 0
    failures: list[str] = []

    for row in rows:
        pos_id = str(row["broker_position_id"] or "").strip()
        order_id = str(row["broker_order_id"] or "").strip()
        client_id = str(row["broker_client_id"] or "").strip()
        mapped = by_id.get(pos_id) or by_client.get(client_id) or by_order.get(order_id)
        now = datetime.now(UTC)
        try:
            if mapped is not None:
                broker_position_id = str(mapped.get("id") or "").strip()
                if not broker_position_id:
                    raise RuntimeError("missing_broker_position_id")
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
                            SET status='closed', closed_at=COALESCE(closed_at,:now),
                                close_reason=:reason, updated_at=:now
                            WHERE id=:id AND user_id=:user_id AND status IN ('open','pending')
                            """
                        ),
                        {
                            "id": row["id"],
                            "user_id": owner_user_id,
                            "now": now,
                            "reason": CLEANUP_REASON,
                        },
                    )
                    session.commit()
                closed += 1
                continue

            if order_id and order_id in active_orders:
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
                            SET status='skipped', closed_at=COALESCE(closed_at,:now),
                                close_reason='superseded_by_newer_provider_signal',
                                updated_at=:now
                            WHERE id=:id AND user_id=:user_id AND status='pending'
                            """
                        ),
                        {"id": row["id"], "user_id": owner_user_id, "now": now},
                    )
                    session.commit()
                cancelled += 1
                continue

            with session_factory() as session:
                if str(row["status"]) == "pending":
                    session.execute(
                        text(
                            """
                            UPDATE positions
                            SET status='skipped', closed_at=COALESCE(closed_at,:now),
                                close_reason='incident_reconciled_no_broker_exposure',
                                updated_at=:now
                            WHERE id=:id AND user_id=:user_id AND status='pending'
                            """
                        ),
                        {"id": row["id"], "user_id": owner_user_id, "now": now},
                    )
                else:
                    session.execute(
                        text(
                            """
                            UPDATE positions
                            SET status='closed', closed_at=COALESCE(closed_at,:now),
                                close_reason='incident_reconciled_no_broker_exposure',
                                updated_at=:now
                            WHERE id=:id AND user_id=:user_id AND status='open'
                            """
                        ),
                        {"id": row["id"], "user_id": owner_user_id, "now": now},
                    )
                session.commit()
            reconciled += 1
        except (MetaApiGatewayError, RuntimeError) as exc:
            failures.append(f"{row['id']}:{getattr(exc, 'code', type(exc).__name__)}")

    await asyncio.sleep(1.0)
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
        print(f"SUPER_SIGNALS_ASIA_OWNER_VERIFY=FAIL_BROKER_READ:{exc.code}", flush=True)
        return

    stale_pos_ids = {str(row["broker_position_id"] or "").strip() for row in rows}
    stale_order_ids = {str(row["broker_order_id"] or "").strip() for row in rows}
    stale_client_ids = {str(row["broker_client_id"] or "").strip() for row in rows}
    stale_pos_ids.discard("")
    stale_order_ids.discard("")
    stale_client_ids.discard("")

    remaining_positions = [
        item for item in after_positions
        if str(item.get("id") or "").strip() in stale_pos_ids
        or str(item.get("orderId") or "").strip() in stale_order_ids
        or str(item.get("clientId") or "").strip() in stale_client_ids
    ]
    remaining_orders = [
        item for item in after_orders
        if str(item.get("id") or "").strip() in stale_order_ids
        or str(item.get("clientId") or "").strip() in stale_client_ids
    ]

    if failures or remaining_positions or remaining_orders:
        print(
            "SUPER_SIGNALS_ASIA_OWNER_VERIFY=FAIL "
            f"rows={len(rows)} failures={len(failures)} "
            f"remaining_positions={len(remaining_positions)} "
            f"remaining_orders={len(remaining_orders)}",
            flush=True,
        )
        return

    print(
        "SUPER_SIGNALS_ASIA_OWNER_BROKER_CLEAN=PASS "
        f"latest_message={latest['provider_message_id']} stale_rows={len(rows)} "
        f"closed={closed} cancelled={cancelled} reconciled={reconciled}",
        flush=True,
    )

    cutoff = datetime.now(UTC)
    reporting_date = cutoff.astimezone(ZoneInfo(TIMEZONE)).date()
    with session_factory() as session:
        clean_cash = session.execute(
            text(
                """
                SELECT COALESCE(SUM(o.cash_pnl),0)
                FROM performance_trade_outcomes o
                WHERE o.user_id=:user_id
                  AND o.closed_at IS NOT NULL
                  AND o.closed_at < :cutoff
                  AND (o.closed_at AT TIME ZONE :timezone)::date=:reporting_date
                  AND (o.source_id IS NULL OR o.source_id<>:asia_source_id)
                  AND o.cash_pnl IS NOT NULL
                """
            ),
            {
                "user_id": owner_user_id,
                "cutoff": cutoff,
                "timezone": TIMEZONE,
                "reporting_date": reporting_date,
                "asia_source_id": source_id,
            },
        ).scalar_one()
        clean_cash = Decimal(str(clean_cash or 0)).quantize(Decimal("0.01"))
        session.execute(
            text(
                """
                INSERT INTO performance_reporting_overrides
                    (user_id, reporting_date, timezone, realised_cash_pnl,
                     reason, incident_key, cutoff_at, created_at, updated_at)
                VALUES
                    (:user_id, :reporting_date, :timezone, :cash,
                     :reason, :incident_key, :cutoff, now(), now())
                ON CONFLICT (user_id, reporting_date) DO UPDATE
                SET timezone=EXCLUDED.timezone,
                    realised_cash_pnl=EXCLUDED.realised_cash_pnl,
                    reason=EXCLUDED.reason,
                    incident_key=EXCLUDED.incident_key,
                    cutoff_at=EXCLUDED.cutoff_at,
                    updated_at=now()
                """
            ),
            {
                "user_id": owner_user_id,
                "reporting_date": reporting_date,
                "timezone": TIMEZONE,
                "cash": clean_cash,
                "reason": (
                    "4 September 2026 TIG Asia Trades management-reader incident: "
                    "quarantine all pre-cleanup Asia outcomes because provider exits and "
                    "close instructions were not reliably actioned. Preserve immutable "
                    "broker evidence and all non-Asia realised P/L; post-cutoff outcomes "
                    "remain live."
                ),
                "incident_key": INCIDENT_KEY,
                "cutoff": cutoff,
            },
        )
        session.commit()

    print(
        f"SUPER_SIGNALS_ASIA_OWNER_REPORTING_QUARANTINE=PASS date={reporting_date} "
        f"clean_non_asia_cash={clean_cash} cutoff={cutoff.isoformat()}",
        flush=True,
    )
