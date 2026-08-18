"""Non-entry readiness cleanup found during the 18 Aug production audit.

These fixes do not alter broker execution policy. They keep the reporting/performance
side healthy so paper results remain usable for provider evaluation:

* Telegram publication formatting is total. Sparse canonical rows such as a provider's
  bare "Buy Gold Now" instruction can be executed using inherited provider context but
  must not crash the publisher merely because the sparse signal row itself has NULL
  entry/SL fields. Missing display-only values render as ``N/A``; nothing is invented.
* ``broker_deals`` is an append-only ledger enforced by a PostgreSQL trigger. Account
  truth sync therefore inserts unseen broker deal IDs and ignores duplicates. It must
  never use ``ON CONFLICT ... DO UPDATE`` against immutable history.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text

_installed = False


def _install_total_telegram_decimal_rendering() -> None:
    import app.telegram_publisher as publisher

    original = publisher._decimal_text
    if getattr(original, "_sparse_safe", False):
        return

    def decimal_text(value: Any) -> str:
        if value is None:
            return "N/A"
        try:
            decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return "N/A"
        normalized = decimal_value.normalize()
        return format(normalized, "f")

    decimal_text._sparse_safe = True  # type: ignore[attr-defined]
    publisher._decimal_text = decimal_text


def _install_append_only_broker_deal_sync() -> None:
    import app.performance_account_truth_override as account_truth

    original = account_truth._store_account_deals
    if getattr(original, "_append_only_safe", False):
        return

    def store_account_deals(
        self: Any,
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
                occurred_at = account_truth._parse_time(payload.get("time"))
                broker_position_id = str(payload.get("positionId") or "").strip() or None
                row = by_broker_position.get(str(broker_position_id)) if broker_position_id else None
                trader = (
                    account_truth.trader_stream_for(
                        str(row["source_alias"] or ""),
                        str(row["original_text"] or ""),
                    )
                    if row is not None
                    else None
                )
                result = session.execute(
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
                        "volume": (
                            account_truth._d(payload.get("volume"))
                            if payload.get("volume") is not None
                            else None
                        ),
                        "price": (
                            account_truth._d(payload.get("price"))
                            if payload.get("price") is not None
                            else None
                        ),
                        "profit": account_truth._d(payload.get("profit")),
                        "commission": account_truth._d(payload.get("commission")),
                        "swap": account_truth._d(payload.get("swap")),
                        "occurred_at": occurred_at,
                        "broker_time": str(payload.get("brokerTime") or "").strip() or None,
                        "raw_payload": json.dumps(payload, separators=(",", ":"), default=str),
                    },
                ).first()
                if result is not None:
                    added += 1
            session.commit()
        return added

    store_account_deals._append_only_safe = True  # type: ignore[attr-defined]
    account_truth._store_account_deals = store_account_deals


def install_aug18_readiness_cleanup() -> None:
    global _installed
    if _installed:
        return
    _install_total_telegram_decimal_rendering()
    _install_append_only_broker_deal_sync()
    _installed = True


__all__ = ["install_aug18_readiness_cleanup"]
