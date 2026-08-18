"""One-time/idempotent repair for TPs erased by the historical breakeven bug.

The Day-27 management path previously sent POSITION_MODIFY with only stopLoss. MetaAPI
interpreted the omitted takeProfit as removal. Day-36 then recorded the disappearance
as an external/manual action. This repair is deliberately evidence-bound: it only acts
on an Owner DEMO position when all of the following are true:

* the local position is still open and its TP is now NULL;
* Day-36 recorded take_profit_changed from a positive old value to NULL;
* the same signal has a successful move_to_break_even management audit within seconds;
* the broker position still exists and currently has no TP.

It restores exactly the old broker TP while preserving the broker's current SL, verifies
the broker state, updates the local mirror, and writes an idempotency audit event.
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

logger = logging.getLogger(__name__)
_REPAIR_EVENT = "mt5.breakeven_tp_repair_success"


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _broker_keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    return tuple(item.strip() for item in raw.split(",") if item.strip())


async def repair_erased_breakeven_take_profits(*, session_factory) -> int:
    owner_raw = os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip()
    keys = _broker_keys()
    if not owner_raw or not keys:
        return 0
    try:
        owner_user_id = UUID(owner_raw)
    except ValueError:
        return 0

    with session_factory() as session:
        account = session.execute(
            text(
                """
                SELECT metaapi_account_id, metaapi_token_ciphertext, account_environment, status
                FROM mt5_accounts
                WHERE owner_user_id=:owner_user_id AND status!='revoked'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"owner_user_id": owner_user_id},
        ).mappings().first()
        candidates = session.execute(
            text(
                """
                SELECT DISTINCT ON (p.id)
                       p.id, p.signal_id, p.broker_position_id,
                       ae.payload->>'old_value' AS desired_tp,
                       ae.created_at AS erased_at
                FROM positions p
                JOIN audit_events ae
                  ON ae.entity_type='position'
                 AND ae.entity_id=p.id
                 AND ae.event_type='mt5.day36_manual_action'
                JOIN audit_events mg
                  ON mg.entity_type='signal'
                 AND mg.entity_id=p.signal_id
                 AND mg.event_type='mt5.day27_management_success'
                WHERE p.user_id=:owner_user_id
                  AND p.status='open'
                  AND p.broker_position_id IS NOT NULL
                  AND p.take_profit IS NULL
                  AND ae.payload->>'action_type'='take_profit_changed'
                  AND ae.payload->>'old_value' IS NOT NULL
                  AND ae.payload->>'new_value' IS NULL
                  AND mg.payload::text LIKE '%move_to_break_even%'
                  AND ae.created_at BETWEEN mg.created_at AND mg.created_at + interval '15 seconds'
                  AND NOT EXISTS (
                      SELECT 1 FROM audit_events repaired
                      WHERE repaired.entity_type='position'
                        AND repaired.entity_id=p.id
                        AND repaired.event_type=:repair_event
                  )
                ORDER BY p.id, ae.created_at DESC
                """
            ),
            {"owner_user_id": owner_user_id, "repair_event": _REPAIR_EVENT},
        ).mappings().all()

    if account is None or not candidates:
        return 0
    if str(account["account_environment"] or "").lower() != "demo":
        logger.error("Breakeven TP repair refused non-demo account")
        return 0
    if str(account["status"] or "") != "connected":
        return 0

    try:
        token = MetaApiTokenCipher(keys).decrypt(bytes(account["metaapi_token_ciphertext"]))
    except BrokerCredentialDecryptionError:
        logger.exception("Breakeven TP repair could not decrypt broker credential")
        return 0

    read = MetaApiReadGateway()
    trade = MetaApiTradeGateway()
    account_id = str(account["metaapi_account_id"])
    try:
        region = await read.resolve_account_region(token=token, account_id=account_id)
        live = await read.read_positions(token=token, account_id=account_id, region=region)
    except MetaApiGatewayError:
        logger.exception("Breakeven TP repair could not read broker positions")
        return 0

    by_id = {str(item.get("id") or "").strip(): item for item in live}
    repaired_count = 0
    for row in candidates:
        position_id = str(row["broker_position_id"] or "").strip()
        desired_tp = _decimal(row["desired_tp"])
        broker = by_id.get(position_id)
        if not position_id or desired_tp is None or broker is None:
            continue
        current_sl = _decimal(broker.get("stopLoss"))
        current_tp = _decimal(broker.get("takeProfit"))
        if current_sl is None:
            logger.error("Breakeven TP repair refused unprotected position id=%s", position_id)
            continue

        try:
            if current_tp is None:
                await trade.modify_position(
                    token=token,
                    account_id=account_id,
                    region=region,
                    position_id=position_id,
                    stop_loss=float(current_sl),
                    take_profit=float(desired_tp),
                )
                verify_rows = await read.read_positions(
                    token=token,
                    account_id=account_id,
                    region=region,
                )
                verified = next(
                    (
                        item
                        for item in verify_rows
                        if str(item.get("id") or "").strip() == position_id
                    ),
                    None,
                )
                if verified is None or _decimal(verified.get("takeProfit")) != desired_tp:
                    logger.error("Breakeven TP repair broker verification failed id=%s", position_id)
                    continue
            else:
                desired_tp = current_tp

            with session_factory() as session:
                session.execute(
                    text(
                        """
                        UPDATE positions
                        SET take_profit=:take_profit, updated_at=now()
                        WHERE id=:position_id AND status='open'
                        """
                    ),
                    {"position_id": row["id"], "take_profit": desired_tp},
                )
                session.execute(
                    text(
                        """
                        INSERT INTO audit_events (
                            actor_user_id, event_type, entity_type, entity_id, payload
                        ) VALUES (
                            :owner_user_id, :event_type, 'position', :position_id,
                            CAST(:payload AS jsonb)
                        )
                        """
                    ),
                    {
                        "owner_user_id": owner_user_id,
                        "event_type": _REPAIR_EVENT,
                        "position_id": row["id"],
                        "payload": json.dumps(
                            {
                                "broker_position_id": position_id,
                                "restored_take_profit": str(desired_tp),
                                "preserved_stop_loss": str(current_sl),
                                "source_bug": "position_modify_omitted_take_profit",
                                "paper_demo_only": True,
                            }
                        ),
                    },
                )
                session.commit()
            repaired_count += 1
        except MetaApiGatewayError:
            logger.exception("Breakeven TP repair broker mutation failed id=%s", position_id)

    if repaired_count:
        logger.warning("Breakeven TP repair restored %d paper position(s)", repaired_count)
    return repaired_count


def install_breakeven_tp_repair() -> None:
    """Run the evidence-bound repair before the production Telegram listener starts."""
    from app.telegram_listener_day38 import PaperPendingAwareListenerManager

    original = PaperPendingAwareListenerManager.start
    if getattr(original, "_breakeven_tp_repair_installed", False):
        return

    async def start(self: Any) -> None:
        try:
            await repair_erased_breakeven_take_profits(
                session_factory=self._session_factory,
            )
        except Exception:
            # Repair must never prevent the reader/router from starting. The positions
            # involved are already protected at breakeven; failure is logged for audit.
            logger.exception("Breakeven TP repair failed safely")
        await original(self)

    start._breakeven_tp_repair_installed = True  # type: ignore[attr-defined]
    PaperPendingAwareListenerManager.start = start


__all__ = ["install_breakeven_tp_repair", "repair_erased_breakeven_take_profits"]
