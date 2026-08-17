"""One-shot operational close for one exact mapped DEMO position.

Runs only when SUPER_SIGNALS_ONE_SHOT_CLOSE_POSITION_ID is set. It fails closed unless
there is exactly one locally mapped OPEN XAUUSD position on the owner's connected DEMO
account and the same broker position is currently open at MetaAPI. No credentials or
secret values are logged and no local trade row is mutated here; normal broker
settlement/reconciliation remains authoritative.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text

from app.db import get_session_factory
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher

logger = logging.getLogger(__name__)


def _position_id(item: dict[str, object]) -> str:
    return str(item.get("id") or item.get("positionId") or "").strip()


async def run() -> None:
    broker_position_id = os.getenv("SUPER_SIGNALS_ONE_SHOT_CLOSE_POSITION_ID", "").strip()
    if not broker_position_id:
        return

    key_value = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    keys = tuple(value.strip() for value in key_value.split(",") if value.strip())
    if not keys:
        raise RuntimeError("one_shot_close_missing_broker_cipher")

    session_factory = get_session_factory()
    with session_factory() as session:
        rows = session.execute(
            text(
                """
                SELECT
                    p.broker_position_id,
                    p.status AS local_status,
                    p.volume AS local_volume,
                    s.symbol,
                    ma.metaapi_account_id,
                    ma.metaapi_token_ciphertext,
                    ma.account_environment,
                    ma.status AS mt5_status
                FROM positions p
                JOIN signals s ON s.id=p.signal_id
                JOIN LATERAL (
                    SELECT
                        metaapi_account_id,
                        metaapi_token_ciphertext,
                        account_environment,
                        status
                    FROM mt5_accounts
                    WHERE owner_user_id=p.user_id
                    ORDER BY created_at DESC
                    LIMIT 1
                ) ma ON true
                WHERE p.broker_position_id=:broker_position_id
                """
            ),
            {"broker_position_id": broker_position_id},
        ).mappings().all()

    if len(rows) != 1:
        raise RuntimeError("one_shot_close_position_not_unique")
    row = rows[0]

    if str(row["local_status"]) != "open":
        logger.info(
            "One-shot close skipped: mapped position=%s is not locally open",
            broker_position_id,
        )
        return
    if str(row["symbol"]).upper() != "XAUUSD":
        raise RuntimeError("one_shot_close_symbol_not_xauusd")
    if str(row["account_environment"]).lower() != "demo":
        raise RuntimeError("one_shot_close_non_demo_blocked")
    if str(row["mt5_status"]).lower() != "connected":
        raise RuntimeError("one_shot_close_mt5_not_connected")

    account_id = str(row["metaapi_account_id"] or "").strip()
    ciphertext = row["metaapi_token_ciphertext"]
    if not account_id or not ciphertext:
        raise RuntimeError("one_shot_close_missing_metaapi_credentials")

    token = MetaApiTokenCipher(keys).decrypt(bytes(ciphertext))
    read_gateway = MetaApiReadGateway()
    trade_gateway = MetaApiTradeGateway()
    region = await read_gateway.resolve_account_region(token=token, account_id=account_id)

    broker_positions = await read_gateway.read_positions(
        token=token,
        account_id=account_id,
        region=region,
    )
    matching = [item for item in broker_positions if _position_id(item) == broker_position_id]
    if not matching:
        logger.info(
            "One-shot close skipped: mapped position=%s is already absent at broker",
            broker_position_id,
        )
        return
    if len(matching) != 1:
        raise RuntimeError("one_shot_close_broker_position_not_unique")

    broker_symbol = str(matching[0].get("symbol") or "").strip().upper()
    if broker_symbol != "XAUUSD":
        raise RuntimeError("one_shot_close_broker_symbol_mismatch")

    logger.warning(
        "One-shot close sending exact DEMO broker position close position=%s symbol=%s",
        broker_position_id,
        broker_symbol,
    )
    await trade_gateway.close_position(
        token=token,
        account_id=account_id,
        region=region,
        position_id=broker_position_id,
    )

    await asyncio.sleep(1.0)
    broker_positions_after = await read_gateway.read_positions(
        token=token,
        account_id=account_id,
        region=region,
    )
    if any(_position_id(item) == broker_position_id for item in broker_positions_after):
        raise RuntimeError("one_shot_close_not_verified")

    logger.warning(
        "One-shot close verified absent at broker position=%s",
        broker_position_id,
    )


if __name__ == "__main__":
    asyncio.run(run())
