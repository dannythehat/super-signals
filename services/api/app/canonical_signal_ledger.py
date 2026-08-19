"""Canonical Signal ledger extensions used by the production decision pipeline.

Provider market signals may legitimately omit an entry price. In that case the canonical
Signal keeps entry_low/entry_high NULL; execution later resolves the fresh broker ask
for BUY or bid for SELL. Pending signals still require literal provider prices.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from app.ai_canonical_signal import (
    AiCanonicalSignalService,
    _ParsedTrade,
    _timestamp_token,
    _token,
)


def _positive(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _ordered_targets(side: str, targets: tuple[Decimal, ...]) -> bool:
    if not targets:
        return False
    if side == "BUY":
        return all(right > left for left, right in zip(targets, targets[1:]))
    if side == "SELL":
        return all(right < left for left, right in zip(targets, targets[1:]))
    return False


class CanonicalSignalLedger(AiCanonicalSignalService):
    @staticmethod
    def _parse_extracted(extracted: dict[str, Any]) -> _ParsedTrade:
        low = _positive(extracted.get("entry_low"))
        high = _positive(extracted.get("entry_high"))
        if low is not None or high is not None:
            return AiCanonicalSignalService._parse_extracted(extracted)

        symbol = str(extracted.get("symbol") or "").strip().upper()
        if symbol == "GOLD":
            symbol = "XAUUSD"
        side = str(extracted.get("side") or "").strip().upper()
        order_type = str(extracted.get("order_type") or "market").strip().lower()
        stop = _positive(extracted.get("stop_loss"))
        targets = tuple(
            parsed
            for parsed in (_positive(value) for value in (extracted.get("take_profits") or []))
            if parsed is not None
        )
        if symbol != "XAUUSD" or side not in {"BUY", "SELL"}:
            raise ValueError("provider_instruction_unsupported")
        if order_type != "market" or stop is None or not _ordered_targets(side, targets):
            raise ValueError("provider_instruction_incomplete")
        size = Decimal("2") if bool(extracted.get("double_lot")) else Decimal("1")
        return _ParsedTrade(
            symbol=symbol,
            side=side,
            order_type="market",
            entry_low=None,  # type: ignore[arg-type]
            entry_high=None,  # type: ignore[arg-type]
            stop_loss=stop,
            take_profits=targets,
            size_multiplier=size,
            has_open_runner=bool(extracted.get("tp_open")),
        )

    @staticmethod
    def _fingerprint(row: Any, trade: _ParsedTrade, revision_index: int) -> str:
        if trade.entry_low is not None or trade.entry_high is not None:
            return AiCanonicalSignalService._fingerprint(row, trade, revision_index)
        payload = {
            "provider_chat_id": int(row["provider_chat_id"]),
            "provider_message_id": int(row["provider_message_id"]),
            "source_revision_index": revision_index,
            "source_posted_at": _timestamp_token(row["source_posted_at"]),
            "symbol": trade.symbol,
            "side": trade.side,
            "order_type": trade.order_type,
            "entry_low": None,
            "entry_high": None,
            "entry_source": "live_executable_price",
            "stop_loss": _token(trade.stop_loss),
            "take_profits": [_token(value) for value in trade.take_profits],
            "has_open_runner": trade.has_open_runner,
            "size_multiplier": _token(trade.size_multiplier),
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()


__all__ = ["CanonicalSignalLedger"]
