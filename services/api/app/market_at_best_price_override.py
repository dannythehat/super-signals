"""Production rule: market signals without an entry use the live executable price.

Provider intent is preserved exactly:
* explicit LIMIT/STOP/PENDING prices remain literal broker-side instructions;
* explicit market prices/zones remain provider evidence;
* a complete market signal with no entry price is not rejected and no price is invented;
  the execution engine uses the fresh broker ask for BUY or bid for SELL immediately
  before sizing and submission.

SL and TP remain mandatory. A no-entry signal is directionally validated against the
fresh executable price before any broker mutation.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from app.ai_message_supervisor import AiMessageDecision
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError

_PENDING_LITERAL = re.compile(
    r"\b(?:BUY|SELL)\s+(?:LIMITS?|STOPS?)\b|\bPENDING\b",
    re.IGNORECASE,
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


def _live_directionally_valid(
    *,
    side: str,
    entry: Decimal,
    stop_loss: Decimal,
    take_profits: tuple[Decimal, ...],
) -> bool:
    if side == "BUY":
        return (
            stop_loss < entry
            and all(tp > entry for tp in take_profits)
            and _ordered_targets(side, take_profits)
        )
    if side == "SELL":
        return (
            stop_loss > entry
            and all(tp < entry for tp in take_profits)
            and _ordered_targets(side, take_profits)
        )
    return False


def _install_critical_entry_absence() -> None:
    """No entry is ordinary market execution, not a critical-entry parser error."""
    import app.critical_entry_policy as critical
    import app.paper_critical_execution as paper_critical
    import app.paper_fresh_start_execution as paper_fresh
    import app.v1_message_policy as v1

    original = critical.parse_critical_entries
    if getattr(original, "_market_absence_supported", False):
        return

    def wrapped(raw_text: str, *, side: str, entry_low: object, entry_high: object):
        low = _positive(entry_low)
        high = _positive(entry_high)
        if low is None and high is None and _PENDING_LITERAL.search(raw_text or "") is None:
            return ()
        return original(
            raw_text,
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
        )

    wrapped._market_absence_supported = True  # type: ignore[attr-defined]
    critical.parse_critical_entries = wrapped
    v1.parse_critical_entries = wrapped
    paper_critical.parse_critical_entries = wrapped
    paper_fresh.parse_critical_entries = wrapped


def _install_v1_live_entry_policy() -> None:
    import app.ai_message_pipeline as pipeline
    import app.v1_message_policy as v1

    original = v1.apply_v1_message_policy
    if getattr(original, "_market_live_entry_supported", False):
        pipeline.apply_v1_message_policy = original
        return

    def wrapped(
        decision: AiMessageDecision,
        *,
        raw_text: str,
        is_edit: bool = False,
        original_has_signal: bool | None = None,
        previous_text: str | None = None,
    ) -> AiMessageDecision:
        result = original(
            decision,
            raw_text=raw_text,
            is_edit=is_edit,
            original_has_signal=original_has_signal,
            previous_text=previous_text,
        )
        if (
            decision.decision != "new_trade"
            or result.action != "skip"
            or result.reason not in {"missing_entry", "signal_entry_invalid"}
            or _PENDING_LITERAL.search(raw_text or "") is not None
        ):
            return result

        extracted = dict(decision.extracted or {})
        # One-sided entry data is malformed. The live-price rule is only for a provider
        # that supplied no entry at all, not for repairing a partially parsed price.
        if _positive(extracted.get("entry_low")) is not None:
            return result
        if _positive(extracted.get("entry_high")) is not None:
            return result

        side = str(extracted.get("side") or "").strip().upper()
        stop = _positive(extracted.get("stop_loss"))
        targets = tuple(
            parsed
            for parsed in (_positive(value) for value in (extracted.get("take_profits") or []))
            if parsed is not None
        )
        if side not in {"BUY", "SELL"} or stop is None or not _ordered_targets(side, targets):
            return result

        # The original policy already passed literal instrument + side gates before it
        # reached missing_entry. Keep SL/TP literal verification here so context/AI can
        # never donate broker prices that the provider did not actually write.
        literals = v1._literal_numbers(raw_text or "")
        required = {stop.normalize(), *(tp.normalize() for tp in targets)}
        if not required.issubset(literals):
            return result

        extracted.update(
            {
                "symbol": "XAUUSD",
                "side": side,
                "order_type": "market",
                "entry_low": None,
                "entry_high": None,
                "stop_loss": format(stop.normalize(), "f"),
                "take_profits": [format(tp.normalize(), "f") for tp in targets],
            }
        )
        return replace(
            decision,
            decision="new_trade",
            action="execute",
            reason="v1_complete_market_signal_live_entry",
            extracted=extracted,
        )

    wrapped._market_live_entry_supported = True  # type: ignore[attr-defined]
    v1.apply_v1_message_policy = wrapped
    # ai_message_pipeline imported the function by name, so patch its bound reference.
    pipeline.apply_v1_message_policy = wrapped


def _install_canonical_nullable_entry() -> None:
    import app.ai_canonical_signal as canonical

    cls = canonical.AiCanonicalSignalService
    original_parse = cls._parse_extracted
    if getattr(original_parse, "_market_live_entry_supported", False):
        return

    def parse_extracted(extracted: dict[str, Any]):
        low = _positive(extracted.get("entry_low"))
        high = _positive(extracted.get("entry_high"))
        if low is not None or high is not None:
            return original_parse(extracted)

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
        if order_type != "market" or stop is None or not targets:
            raise ValueError("provider_instruction_incomplete")
        if not _ordered_targets(side, targets):
            raise ValueError("provider_instruction_incomplete")
        size = Decimal("2") if bool(extracted.get("double_lot")) else Decimal("1")
        return canonical._ParsedTrade(
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

    parse_extracted._market_live_entry_supported = True  # type: ignore[attr-defined]
    cls._parse_extracted = staticmethod(parse_extracted)

    original_fingerprint = cls._fingerprint

    def fingerprint(row: Any, trade: Any, revision_index: int) -> str:
        if trade.entry_low is not None or trade.entry_high is not None:
            return original_fingerprint(row, trade, revision_index)
        payload = {
            "provider_chat_id": int(row["provider_chat_id"]),
            "provider_message_id": int(row["provider_message_id"]),
            "source_revision_index": revision_index,
            "source_posted_at": canonical._timestamp_token(row["source_posted_at"]),
            "symbol": trade.symbol,
            "side": trade.side,
            "order_type": trade.order_type,
            "entry_low": None,
            "entry_high": None,
            "entry_source": "live_executable_price",
            "stop_loss": canonical._token(trade.stop_loss),
            "take_profits": [canonical._token(value) for value in trade.take_profits],
            "has_open_runner": trade.has_open_runner,
            "size_multiplier": canonical._token(trade.size_multiplier),
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()

    cls._fingerprint = staticmethod(fingerprint)


def _install_execution_live_entry() -> None:
    import app.day28_zone_guard as day28
    import app.mt5_execution_day26 as day26

    cls = day26.Day26Mt5ExecutionService
    original_required = cls._required_decimal
    if getattr(original_required, "_market_live_entry_supported", False):
        return

    def required_decimal(value: object, code: str) -> Decimal:
        if code == "signal_entry_invalid" and value is None:
            return Decimal("0")
        return original_required(value, code)

    required_decimal._market_live_entry_supported = True  # type: ignore[attr-defined]
    cls._required_decimal = staticmethod(required_decimal)

    original_directional = cls._directionally_valid

    def directional(
        *,
        side: str,
        entry_low: Decimal,
        entry_high: Decimal,
        stop_loss: Decimal,
        take_profits: tuple[Decimal, ...],
    ) -> bool:
        if entry_low == 0 and entry_high == 0:
            # Final stop/TP geometry is checked against the fresh broker price in
            # _resolve_entry below. Here only reject internally inconsistent TP order.
            return stop_loss > 0 and _ordered_targets(side, take_profits)
        return original_directional(
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
        )

    cls._directionally_valid = staticmethod(directional)

    original_resolve = cls._resolve_entry

    async def resolve_entry(self: Any, *, owner_user_id, signal, day23, initial_state):
        if signal.entry_low == 0 and signal.entry_high == 0:
            try:
                executable = Decimal(
                    str(Day23Mt5ReadService.executable_price(initial_state, signal.side))
                )
            except Day23ReadError as exc:
                raise Day26ExecutionError(exc.code) from exc
            if not _live_directionally_valid(
                side=signal.side,
                entry=executable,
                stop_loss=signal.stop_loss,
                take_profits=signal.take_profits,
            ):
                raise Day26ExecutionError("strict_directional_validation_failed")
            return executable, initial_state

        executable, state = await original_resolve(
            self,
            owner_user_id=owner_user_id,
            signal=signal,
            day23=day23,
            initial_state=initial_state,
        )
        if not _live_directionally_valid(
            side=signal.side,
            entry=executable,
            stop_loss=signal.stop_loss,
            take_profits=signal.take_profits,
        ):
            raise Day26ExecutionError("strict_directional_validation_failed")
        return executable, state

    cls._resolve_entry = resolve_entry

    original_provider_zone = day28.Day28GuardedExecutionService._provider_zone

    def provider_zone(self: Any, signal_id: Any) -> tuple[Decimal, Decimal]:
        with self._day28_session_factory() as session:
            from sqlalchemy import text

            row = session.execute(
                text("SELECT entry_low, entry_high FROM signals WHERE id=:signal_id LIMIT 1"),
                {"signal_id": signal_id},
            ).mappings().first()
        if row is not None and row["entry_low"] is None and row["entry_high"] is None:
            return Decimal("0"), Decimal("0")
        return original_provider_zone(self, signal_id)

    day28.Day28GuardedExecutionService._provider_zone = provider_zone


def install_market_at_best_price_override() -> None:
    """Install the product rule across interpretation, ledger and execution."""
    _install_critical_entry_absence()
    _install_v1_live_entry_policy()
    _install_canonical_nullable_entry()
    _install_execution_live_entry()


__all__ = ["install_market_at_best_price_override"]
