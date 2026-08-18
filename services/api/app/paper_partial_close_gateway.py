"""Demo-only exact-ID partial close adapter for paper testing.

This module cannot mutate a live account. The persisted account environment must be
``demo`` before a MetaAPI POSITION_PARTIAL request is emitted. Volume selection stays
outside the gateway so broker minimum/step rules can be validated before this call.
"""

from __future__ import annotations

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_trade_gateway import MetaApiTradeGateway


class PaperPartialCloseGateway:
    def __init__(self, base: MetaApiTradeGateway) -> None:
        self._base = base

    async def close_partial(
        self,
        *,
        account_environment: str,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
        volume: float,
    ) -> None:
        if account_environment.strip().lower() != "demo":
            raise MetaApiGatewayError("paper_partial_demo_account_required")
        normalized_position_id = position_id.strip()
        if not normalized_position_id:
            raise MetaApiGatewayError("broker_position_id_invalid")
        if not self._base._positive_finite(volume):  # noqa: SLF001 - guarded adapter
            raise MetaApiGatewayError("trade_request_invalid")

        await self._base._trade_request(  # noqa: SLF001 - guarded adapter
            token=token,
            account_id=account_id,
            region=self._base._normalize_region(region),  # noqa: SLF001
            json_body={
                "actionType": "POSITION_PARTIAL",
                "positionId": normalized_position_id,
                "volume": float(volume),
            },
        )


__all__ = ["PaperPartialCloseGateway"]
