"""Canonical Signal ledger extensions used by the production decision pipeline.

Provider market signals may legitimately omit an entry price. In that case the canonical
Signal keeps entry_low/entry_high NULL; execution later resolves the fresh broker ask
for BUY or bid for SELL. The exact standalone bare-Gold-NOW profile also keeps provider
SL/TP NULL because its protection is execution-derived, not provider-supplied.

Exact same-source trade geometry repeated within a very short provider burst is one
logical signal, not two broker entries. This protects feeds which publish a concise signal
and an analysis companion at the same time while preserving genuinely distinct entries.

Recovery/idempotency helpers are explicit methods of this canonical ledger. Production
therefore never depends on removed override modules for signal lookup or observations.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.ai_canonical_signal import (
    AiCanonicalSignalService,
    AiSignalResult,
    _ParsedTrade,
    _timestamp_token,
    _token,
)
from app.bare_gold_now_policy import PROFILE

_DUPLICATE_BURST_SECONDS = 15


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
    """Single production signal ledger, including pre-execution provider revisions."""

    def signal_id_for_message(self, message_id: UUID) -> UUID | None:
        with self._session_factory() as session:
            value = session.execute(
                text(
                    """
                    SELECT id FROM signals
                    WHERE source_message_id=:message_id
                    ORDER BY created_at ASC
                    LIMIT 1
                    """
                ),
                {"message_id": message_id},
            ).scalar_one_or_none()
        return UUID(str(value)) if value is not None else None

    def record_observation(
        self,
        *,
        signal_id: UUID,
        message_id: UUID,
        revision_index: int,
        disposition: str,
        fingerprint: str | None,
    ) -> None:
        with self._session_factory() as session:
            self._observe(
                session,
                signal_id=signal_id,
                message_id=message_id,
                revision_index=revision_index,
                fingerprint=fingerprint,  # type: ignore[arg-type]
                disposition=disposition,
            )
            session.commit()

    def process(
        self,
        *,
        message_id: UUID,
        extracted: dict[str, Any],
        revision_index: int = 0,
    ) -> AiSignalResult:
        """Create a signal unless the provider just emitted identical trade geometry."""
        try:
            trade = self._parse_extracted(extracted)
        except ValueError as exc:
            return AiSignalResult(False, False, None, str(exc))

        with self._session_factory() as session:
            row = self._message_revision_row(session, message_id, revision_index)
            if row is None:
                return AiSignalResult(False, False, None, "message_not_eligible")
            posted_at = row["source_posted_at"]
            if trade.entry_low is not None and trade.entry_high is not None:
                recent = session.execute(
                    text(
                        """
                        SELECT id
                        FROM signals
                        WHERE source_id=:source_id
                          AND source_message_id<>:message_id
                          AND parser_status='accepted'
                          AND source_posted_at>=:burst_start
                          AND source_posted_at<=:posted_at
                          AND symbol=:symbol
                          AND side=:side
                          AND order_type=:order_type
                          AND entry_low=:entry_low
                          AND entry_high=:entry_high
                          AND stop_loss=:stop_loss
                          AND take_profits=CAST(:take_profits AS jsonb)
                          AND has_open_runner=:has_open_runner
                          AND risk_multiplier=:risk_multiplier
                        ORDER BY source_posted_at DESC, created_at DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "source_id": row["source_id"],
                        "message_id": row["message_id"],
                        "burst_start": posted_at - timedelta(seconds=_DUPLICATE_BURST_SECONDS),
                        "posted_at": posted_at,
                        "symbol": trade.symbol,
                        "side": trade.side,
                        "order_type": trade.order_type,
                        "entry_low": trade.entry_low,
                        "entry_high": trade.entry_high,
                        "stop_loss": trade.stop_loss,
                        "take_profits": json.dumps([_token(value) for value in trade.take_profits]),
                        "has_open_runner": trade.has_open_runner,
                        "risk_multiplier": trade.size_multiplier,
                    },
                ).scalar_one_or_none()
                if recent is not None:
                    fingerprint = self._fingerprint(row, trade, revision_index)
                    self._observe(
                        session,
                        signal_id=recent,
                        message_id=row["message_id"],
                        revision_index=revision_index,
                        fingerprint=fingerprint,
                        disposition="duplicate",
                    )
                    session.commit()
                    return AiSignalResult(
                        False,
                        True,
                        UUID(str(recent)),
                        "recent_same_source_trade",
                    )

        return super().process(
            message_id=message_id,
            extracted=extracted,
            revision_index=revision_index,
        )

    def revise(
        self,
        *,
        message_id,
        extracted: dict[str, Any],
        revision_index: int,
        allow_revision: bool,
        reason: str,
    ) -> AiSignalResult:
        """Apply one provider edit to the existing canonical signal."""
        return self.apply_pre_execution_revision(
            message_id=message_id,
            extracted=extracted,
            revision_index=revision_index,
            execute=allow_revision,
            reason=reason,
        )

    @staticmethod
    def _parse_extracted(extracted: dict[str, Any]) -> _ParsedTrade:
        profile = str(extracted.get("execution_profile") or "")
        if profile == PROFILE:
            symbol = str(extracted.get("symbol") or "").strip().upper()
            if symbol == "GOLD":
                symbol = "XAUUSD"
            side = str(extracted.get("side") or "").strip().upper()
            if symbol != "XAUUSD" or side not in {"BUY", "SELL"}:
                raise ValueError("provider_instruction_unsupported")
            return _ParsedTrade(
                symbol="XAUUSD",
                side=side,
                order_type="market",
                entry_low=None,  # type: ignore[arg-type]
                entry_high=None,  # type: ignore[arg-type]
                stop_loss=None,  # type: ignore[arg-type]
                take_profits=(),
                size_multiplier=Decimal("1"),
                has_open_runner=False,
            )

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
            "stop_loss": _token(trade.stop_loss) if trade.stop_loss is not None else None,
            "take_profits": [_token(value) for value in trade.take_profits],
            "has_open_runner": trade.has_open_runner,
            "size_multiplier": _token(trade.size_multiplier),
            "execution_profile": (
                PROFILE if trade.stop_loss is None and not trade.take_profits else None
            ),
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()


__all__ = ["CanonicalSignalLedger"]
