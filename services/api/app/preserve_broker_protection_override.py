"""Preserve the untouched protection leg on every MetaAPI POSITION_MODIFY.

MetaAPI's POSITION_MODIFY endpoint treats an omitted stopLoss/takeProfit as removal of
that field. Super Signals management historically sent only the field being changed,
so a provider instruction such as "move SL to breakeven" could accidentally erase the
existing TP. This production override makes the broker mutation atomic and lossless:

* changing SL re-sends the broker's current TP when one exists;
* changing TP re-sends the broker's current SL when one exists;
* a genuine runner with no TP remains without a TP;
* if the position cannot be re-read, fail closed before mutation.

The read happens immediately before the mutation so stale local database values cannot
reintroduce a protection level the broker no longer has.
"""

from __future__ import annotations

import math
from typing import Any

from app.metaapi_gateway import MetaApiGatewayError


def _positive(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def install_preserve_broker_protection_override() -> None:
    from app.metaapi_read_gateway import MetaApiReadGateway
    from app.metaapi_trade_gateway import MetaApiTradeGateway

    original = MetaApiTradeGateway.modify_position
    if getattr(original, "_preserves_broker_protection", False):
        return

    async def modify_position(
        self: Any,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> None:
        if stop_loss is None and take_profit is None:
            raise MetaApiGatewayError("trade_request_invalid")

        # MetaAPI POSITION_MODIFY is replacement-like: an omitted counterpart may be
        # cleared. Read broker state immediately before mutation and preserve it.
        if stop_loss is None or take_profit is None:
            positions = await MetaApiReadGateway().read_positions(
                token=token,
                account_id=account_id,
                region=region,
            )
            broker = next(
                (
                    item
                    for item in positions
                    if str(item.get("id") or "").strip() == position_id.strip()
                ),
                None,
            )
            if broker is None:
                raise MetaApiGatewayError("broker_position_mapping_missing")
            if stop_loss is None:
                stop_loss = _positive(broker.get("stopLoss"))
            if take_profit is None:
                take_profit = _positive(broker.get("takeProfit"))

        await original(
            self,
            token=token,
            account_id=account_id,
            region=region,
            position_id=position_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    modify_position._preserves_broker_protection = True  # type: ignore[attr-defined]
    MetaApiTradeGateway.modify_position = modify_position


__all__ = ["install_preserve_broker_protection_override"]
