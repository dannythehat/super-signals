"""Emergency 2026-09-04 Asia lifecycle incident guard.

This module is loaded by Python from PYTHONPATH in the Render runtime. It is deliberately
inactive during builds/tests and activates only when the explicit incident flag is set.

Two responsibilities:
1. Hotfix provider lifecycle wording that the canonical policy was incorrectly treating
   as result-only (notably `Out at BE` and `Close fully with -30pips`).
2. One time, broker-authoritative cleanup of stale TIG Asia Trades exposure older than
   that provider's newest accepted XAUUSD signal, followed by an audited reporting
   quarantine so incident-corrupted Asia P/L cannot distort daily/weekly/monthly totals.

Broker evidence is never deleted or rewritten. Exact mapped broker position/order IDs
are used for every mutation. The reporting override preserves forensic truth while
excluding the contaminated pre-cutoff Asia outcomes from user-facing aggregates.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

_INCIDENT_ENV = "SUPER_SIGNALS_ASIA_INCIDENT_20260904"
_INCIDENT_KEY = "incident-2026-09-04-asia-management-reader"
_CLEANUP_REASON = "incident_asia_stale_cleanup_20260904"
_TIMEZONE = "Europe/Sofia"


def _runtime_enabled() -> bool:
    return (
        os.getenv(_INCIDENT_ENV, "").strip() == "1"
        and os.getcwd() == "/app"
        and os.getenv("PYTHONPATH", "").find("/app/services/api") >= 0
    )


def _install_reader_hotfix() -> None:
    """Patch the management extractor before v1_message_policy imports it."""
    from app import day27_management_policy as policy

    original = policy.extract_day27_management_actions
    result_type = policy.Day27ManagementPolicyResult

    out_at_be = re.compile(
        r"^\s*OUT\s+AT\s+(?:BE|BREAKEVEN|BREAK\s+EVEN)\b",
        re.IGNORECASE | re.DOTALL,
    )
    close_fully = re.compile(
        r"\bCLOSE\s+FULLY\b(?:\s+(?:WITH|AT|FOR)\s+[+-]?\d+(?:\.\d+)?\s*PIPS?\b)?",
        re.IGNORECASE,
    )
    book_some_profits = re.compile(
        r"\bBOOK\s+(?:SOME\s+)?PROFITS?\b",
        re.IGNORECASE,
    )
    explicit_partial = re.compile(
        r"\b(?:BOOK|TAKE)\s+PARTIAL\b|\bTP\s*\d+\b.*\bHIT\b",
        re.IGNORECASE | re.DOTALL,
    )

    def incident_extract(raw_text: str):
        text = (raw_text or "").strip()
        if out_at_be.search(text):
            return result_type(
                ({"type": "close", "target": "all", "value": None},),
                "incident_provider_exit_out_at_be",
            )
        if close_fully.search(text):
            return result_type(
                ({"type": "close", "target": "all", "value": None},),
                "incident_provider_explicit_close_fully",
            )
        if book_some_profits.search(text) and not explicit_partial.search(text):
            return result_type(
                ({"type": "close", "target": "profitable_only", "value": None},),
                "incident_profit_qualified_close",
            )
        return original(raw_text)

    policy.extract_day27_management_actions = incident_extract
    print("SUPER_SIGNALS_ASIA_READER_HOTFIX=ACTIVE", flush=True)


async def _cleanup_stale_asia_exposure() -> None:
    from sqlalchemy import text

    from app.db import get_session_factory
    from app.metaapi_gateway import MetaApiGatewayError
    from app.metaapi_read_gateway import MetaApiReadGateway
    from app.metaapi_trade_gateway import MetaApiTradeGateway
    from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

    session_factory = get_session_factory()

    # Idempotency: once the audited incident override exists, never mutate the broker
    # again from this one-shot cleanup. Reader hotfix remains active independently.
    with session_factory() as session:
        already_done = session.execute(
            text(
                "SELECT 1 FROM performance_reporting_overrides "
                "WHERE incident_key=:incident_key LIMIT 1"
            ),
            {"incident_key": _INCIDENT_KEY},
        ).scalar_one_or_none()
        if already_done is not None:
            print("SUPER_SIGNALS_ASIA_INCIDENT_CLEANUP=ALREADY_DONE", flush=True)
            return

        account = session.execute(
            text(
                """
                SELECT owner_user_id, metaapi_account_id, metaapi_token_ciphertext
                FROM mt5_accounts
                WHERE status='connected' AND account_environment='demo'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
        source = session.execute(
            text(
                """
                SELECT id
                FROM sources
                WHERE status<>'revoked'
                  AND (
                    source_alias ILIKE '%Asia Trades%'
                    OR chat_title ILIKE '%Asia Trades%'
                  )
                ORDER BY updated_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()

    if account is None or source is None:
        print("SUPER_SIGNALS_ASIA_INCIDENT_CLEANUP=SKIP_MISSING_ACCOUNT_OR_SOURCE", flush=True)
        return

    owner_user_id = account["owner_user_id"]
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
            print("SUPER_SIGNALS_ASIA_INCIDENT_CLEANUP=SKIP_NO_LATEST_SIGNAL", flush=True)
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

    if not rows:
        print(
            f"SUPER_SIGNALS_ASIA_INCIDENT_CLEANUP=NO_STALE latest_message={latest['provider_message_id']}",
            flush=True,
        )
    else:
        key_value = (
            os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
            or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
            or ""
        )
        keys = tuple(value.strip() for value in key_value.split(",") if value.strip())
        if not keys:
            print("SUPER_SIGNALS_ASIA_INCIDENT_CLEANUP=FAIL_NO_BROKER_KEYS", flush=True)
            return

        cipher = MetaApiTokenCipher(keys)
        try:
            token = cipher.decrypt(bytes(account["metaapi_token_ciphertext"])).strip()
        except BrokerCredentialDecryptionError:
            print("SUPER_SIGNALS_ASIA_INCIDENT_CLEANUP=FAIL_DECRYPT", flush=True)
            return

        read = MetaApiReadGateway()
        trade = MetaApiTradeGateway()
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
            print(f"SUPER_SIGNALS_ASIA_INCIDENT_CLEANUP=FAIL_BROKER_READ:{exc.code}", flush=True)
            return

        open_by_id = {
            str(item.get("id") or "").strip(): item
            for item in broker_positions
            if str(item.get("id") or "").strip()
        }
        open_by_client = {
            str(item.get("clientId") or "").strip(): item
            for item in broker_positions
            if str(item.get("clientId") or "").strip()
        }
        open_by_order = {
            str(item.get("orderId") or "").strip(): item
            for item in broker_positions
            if str(item.get("orderId") or "").strip()
        }
        active_order_ids = {
            str(item.get("id") or "").strip()
            for item in broker_orders
            if str(item.get("id") or "").strip()
        }

        closed_count = 0
        cancelled_count = 0
        local_terminal_count = 0
        failed: list[str] = []
        now = datetime.now(UTC)

        for row in rows:
            local_id = str(row["id"])
            broker_position_id = str(row["broker_position_id"] or "").strip()
            broker_order_id = str(row["broker_order_id"] or "").strip()
            broker_client_id = str(row["broker_client_id"] or "").strip()
            mapped_broker_position = (
                open_by_id.get(broker_position_id)
                or open_by_client.get(broker_client_id)
                or open_by_order.get(broker_order_id)
            )

            try:
                if mapped_broker_position is not None:
                    actual_position_id = str(mapped_broker_position.get("id") or "").strip()
                    if not actual_position_id:
                        raise RuntimeError("mapped_position_id_missing")
                    await trade.close_position(
                        token=token,
                        account_id=str(account["metaapi_account_id"]),
                        region=region,
                        position_id=actual_position_id,
                    )
                    with session_factory() as session:
                        session.execute(
                            text(
                                """
                                UPDATE positions
                                SET status='closed', closed_at=COALESCE(closed_at,:now),
                                    close_reason=:reason, updated_at=:now
                                WHERE id=:id AND status IN ('open','pending')
                                """
                            ),
                            {"id": row["id"], "now": now, "reason": _CLEANUP_REASON},
                        )
                        session.commit()
                    closed_count += 1
                    continue

                if broker_order_id and broker_order_id in active_order_ids:
                    await trade.cancel_order(
                        token=token,
                        account_id=str(account["metaapi_account_id"]),
                        region=region,
                        order_id=broker_order_id,
                    )
                    with session_factory() as session:
                        session.execute(
                            text(
                                """
                                UPDATE positions
                                SET status='skipped', closed_at=COALESCE(closed_at,:now),
                                    close_reason='superseded_by_newer_provider_signal',
                                    updated_at=:now
                                WHERE id=:id AND status='pending'
                                """
                            ),
                            {"id": row["id"], "now": now},
                        )
                        session.commit()
                    cancelled_count += 1
                    continue

                # No broker exposure remains for this mapped stale row. Terminalise
                # the local stale record so the UI cannot continue showing it open.
                with session_factory() as session:
                    if str(row["status"]) == "pending":
                        session.execute(
                            text(
                                """
                                UPDATE positions
                                SET status='skipped', closed_at=COALESCE(closed_at,:now),
                                    close_reason='incident_reconciled_no_broker_exposure',
                                    updated_at=:now
                                WHERE id=:id AND status='pending'
                                """
                            ),
                            {"id": row["id"], "now": now},
                        )
                    else:
                        session.execute(
                            text(
                                """
                                UPDATE positions
                                SET status='closed', closed_at=COALESCE(closed_at,:now),
                                    close_reason='incident_reconciled_no_broker_exposure',
                                    updated_at=:now
                                WHERE id=:id AND status='open'
                                """
                            ),
                            {"id": row["id"], "now": now},
                        )
                    session.commit()
                local_terminal_count += 1
            except (MetaApiGatewayError, RuntimeError) as exc:
                code = getattr(exc, "code", type(exc).__name__)
                failed.append(f"{local_id}:{code}")

        # Verify broker truth after mutations. Exact IDs/client IDs/order IDs from the
        # stale set must not remain exposed.
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
            print(f"SUPER_SIGNALS_ASIA_INCIDENT_VERIFY=FAIL_BROKER_READ:{exc.code}", flush=True)
            return

        stale_position_ids = {str(row["broker_position_id"] or "").strip() for row in rows}
        stale_order_ids = {str(row["broker_order_id"] or "").strip() for row in rows}
        stale_client_ids = {str(row["broker_client_id"] or "").strip() for row in rows}
        stale_position_ids.discard("")
        stale_order_ids.discard("")
        stale_client_ids.discard("")

        remaining_positions = [
            item
            for item in after_positions
            if str(item.get("id") or "").strip() in stale_position_ids
            or str(item.get("orderId") or "").strip() in stale_order_ids
            or str(item.get("clientId") or "").strip() in stale_client_ids
        ]
        remaining_orders = [
            item
            for item in after_orders
            if str(item.get("id") or "").strip() in stale_order_ids
            or str(item.get("clientId") or "").strip() in stale_client_ids
        ]
        if remaining_positions or remaining_orders or failed:
            print(
                "SUPER_SIGNALS_ASIA_INCIDENT_VERIFY=FAIL "
                f"remaining_positions={len(remaining_positions)} "
                f"remaining_orders={len(remaining_orders)} failed={len(failed)}",
                flush=True,
            )
            return

        print(
            "SUPER_SIGNALS_ASIA_INCIDENT_BROKER_CLEAN=PASS "
            f"latest_message={latest['provider_message_id']} stale_rows={len(rows)} "
            f"closed={closed_count} cancelled={cancelled_count} "
            f"local_terminal={local_terminal_count}",
            flush=True,
        )

    # Quarantine all pre-cutoff Asia outcomes for this local day. Preserve all other
    # providers' broker-realised P/L. Future outcomes after the cutoff continue to be
    # counted normally by reporting_overrides.py.
    cutoff = datetime.now(UTC)
    reporting_date = cutoff.astimezone(ZoneInfo(_TIMEZONE)).date()
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
                "timezone": _TIMEZONE,
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
                "timezone": _TIMEZONE,
                "cash": clean_cash,
                "reason": (
                    "4 September 2026 Asia management-reader incident: quarantine all "
                    "pre-cleanup TIG Asia Trades outcomes because provider exits/close "
                    "instructions were not reliably actioned. Preserve immutable broker "
                    "evidence and all non-Asia realised P/L; post-cutoff outcomes remain live."
                ),
                "incident_key": _INCIDENT_KEY,
                "cutoff": cutoff,
            },
        )
        session.commit()

    print(
        f"SUPER_SIGNALS_ASIA_REPORTING_QUARANTINE=PASS date={reporting_date} "
        f"clean_non_asia_cash={clean_cash} cutoff={cutoff.isoformat()}",
        flush=True,
    )


if _runtime_enabled():
    try:
        _install_reader_hotfix()
        asyncio.run(_cleanup_stale_asia_exposure())
    except Exception as exc:  # fail startup open, but make incident failure explicit in logs
        print(
            f"SUPER_SIGNALS_ASIA_INCIDENT_FATAL={type(exc).__name__}:{str(exc)[:180]}",
            flush=True,
        )
