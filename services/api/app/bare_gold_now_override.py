"""Exact bare Gold NOW execution profile for Owner paper trading.

This rule is intentionally narrow. It applies only to a standalone immediate provider
command such as ``BUY GOLD NOW`` / ``SELL GOLD NOW`` (including XAUUSD and reversed
word order). It does not alter any normal signal that contains provider prices, SL, TP,
ranges, pending orders or other text.

For this one profile only, Super Signals enters at the fresh executable market price and
attaches paper protection derived from that price:
* TP: 50 provider Gold pips = 5.0 XAUUSD price units
* SL: 100 provider Gold pips = 10.0 XAUUSD price units

The canonical signal keeps provider SL/TP absent; the derived protection is stored on
the resulting position, so audit evidence never pretends the provider supplied numbers
that were not in the Telegram message.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from typing import Any

from app.ai_message_supervisor import AiMessageDecision
from app.mt5_execution_day26 import Day26ExecutionError
from app.mt5_read_service_day23 import Day23Mt5ReadService, Day23ReadError

PROFILE = "bare_gold_now_50_100"
GOLD_PROVIDER_PIP = Decimal("0.1")
TAKE_PROFIT_PIPS = Decimal("50")
STOP_LOSS_PIPS = Decimal("100")
TAKE_PROFIT_DISTANCE = GOLD_PROVIDER_PIP * TAKE_PROFIT_PIPS
STOP_LOSS_DISTANCE = GOLD_PROVIDER_PIP * STOP_LOSS_PIPS

_BARE_NOW = re.compile(
    r"^\s*(?:(BUY|SELL)\s+(?:GOLD|XAUUSD)|(?:GOLD|XAUUSD)\s+(BUY|SELL))\s+NOW\s*[.!🔥✅🚨⚡]*\s*$",
    re.IGNORECASE,
)


def bare_now_side(raw_text: str) -> str | None:
    match = _BARE_NOW.fullmatch(raw_text or "")
    if match is None:
        return None
    return (match.group(1) or match.group(2) or "").upper() or None


def _install_v1_policy() -> None:
    import app.ai_message_pipeline as pipeline
    import app.v1_message_policy as v1

    original = v1.apply_v1_message_policy
    if getattr(original, "_bare_gold_now_supported", False):
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
        side = bare_now_side(raw_text)
        if side is None or is_edit or decision.decision != "new_trade":
            return result

        extracted = dict(decision.extracted or {})
        extracted.update(
            {
                "symbol": "XAUUSD",
                "side": side,
                "order_type": "market",
                "entry_low": None,
                "entry_high": None,
                "stop_loss": None,
                "take_profits": [],
                "double_lot": False,
                "tp_open": False,
                "execution_profile": PROFILE,
            }
        )
        return replace(
            decision,
            decision="new_trade",
            action="execute",
            reason=PROFILE,
            extracted=extracted,
        )

    wrapped._bare_gold_now_supported = True  # type: ignore[attr-defined]
    v1.apply_v1_message_policy = wrapped
    pipeline.apply_v1_message_policy = wrapped


def _install_canonical_profile() -> None:
    import app.ai_canonical_signal as canonical

    cls = canonical.AiCanonicalSignalService
    original_parse = cls._parse_extracted
    if getattr(original_parse, "_bare_gold_now_supported", False):
        return

    def parse_extracted(extracted: dict[str, Any]):
        if extracted.get("execution_profile") != PROFILE:
            return original_parse(extracted)
        side = str(extracted.get("side") or "").strip().upper()
        symbol = str(extracted.get("symbol") or "").strip().upper()
        if symbol == "GOLD":
            symbol = "XAUUSD"
        if symbol != "XAUUSD" or side not in {"BUY", "SELL"}:
            raise ValueError("provider_instruction_unsupported")
        return canonical._ParsedTrade(
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

    parse_extracted._bare_gold_now_supported = True  # type: ignore[attr-defined]
    cls._parse_extracted = staticmethod(parse_extracted)

    original_fingerprint = cls._fingerprint

    def fingerprint(row: Any, trade: Any, revision_index: int) -> str:
        if not (
            trade.entry_low is None
            and trade.entry_high is None
            and trade.stop_loss is None
            and not trade.take_profits
        ):
            return original_fingerprint(row, trade, revision_index)
        payload = {
            "provider_chat_id": int(row["provider_chat_id"]),
            "provider_message_id": int(row["provider_message_id"]),
            "source_revision_index": revision_index,
            "source_posted_at": canonical._timestamp_token(row["source_posted_at"]),
            "symbol": trade.symbol,
            "side": trade.side,
            "order_type": "market",
            "entry_source": "live_executable_price",
            "execution_profile": PROFILE,
            "provider_stop_loss": None,
            "provider_take_profits": [],
        }
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()

    cls._fingerprint = staticmethod(fingerprint)


def _install_execution_profile() -> None:
    import app.mt5_execution_day26 as day26
    from app.paper_execution_priority import PaperExecutionPriorityService

    cls = day26.Day26Mt5ExecutionService
    original_required = cls._required_decimal
    if not getattr(original_required, "_bare_gold_now_supported", False):
        def required_decimal(value: object, code: str) -> Decimal:
            if code == "signal_stop_loss_invalid" and value is None:
                return Decimal("0")
            return original_required(value, code)

        required_decimal._bare_gold_now_supported = True  # type: ignore[attr-defined]
        cls._required_decimal = staticmethod(required_decimal)

    original_directional = cls._directionally_valid
    if not getattr(original_directional, "_bare_gold_now_supported", False):
        def directional(
            *,
            side: str,
            entry_low: Decimal,
            entry_high: Decimal,
            stop_loss: Decimal,
            take_profits: tuple[Decimal, ...],
        ) -> bool:
            if (
                side in {"BUY", "SELL"}
                and entry_low == 0
                and entry_high == 0
                and stop_loss == 0
                and not take_profits
            ):
                return True
            return original_directional(
                side=side,
                entry_low=entry_low,
                entry_high=entry_high,
                stop_loss=stop_loss,
                take_profits=take_profits,
            )

        directional._bare_gold_now_supported = True  # type: ignore[attr-defined]
        cls._directionally_valid = staticmethod(directional)

    original_resolve = PaperExecutionPriorityService._resolve_entry
    if getattr(original_resolve, "_bare_gold_now_supported", False):
        return

    async def resolve_entry(self: Any, *, owner_user_id, signal, day23, initial_state):
        with self._session_factory() as session:
            from sqlalchemy import text

            raw_text = session.execute(
                text("SELECT original_text FROM signals WHERE id=:signal_id LIMIT 1"),
                {"signal_id": signal.signal_id},
            ).scalar_one_or_none()
        side = bare_now_side(str(raw_text or ""))
        if side is None:
            return await original_resolve(
                self,
                owner_user_id=owner_user_id,
                signal=signal,
                day23=day23,
                initial_state=initial_state,
            )
        if side != signal.side:
            raise Day26ExecutionError("bare_gold_now_side_mismatch")

        self._assert_signal_recent(signal)
        try:
            executable = Decimal(
                str(Day23Mt5ReadService.executable_price(initial_state, signal.side))
            )
        except Day23ReadError as exc:
            raise Day26ExecutionError(exc.code) from exc

        if side == "BUY":
            stop_loss = executable - STOP_LOSS_DISTANCE
            take_profit = executable + TAKE_PROFIT_DISTANCE
        else:
            stop_loss = executable + STOP_LOSS_DISTANCE
            take_profit = executable - TAKE_PROFIT_DISTANCE
        if stop_loss <= 0 or take_profit <= 0:
            raise Day26ExecutionError("bare_gold_now_protection_invalid")

        # _SignalInput is frozen, but this request-local instance has not yet been used
        # for sizing/planning/submission. Mutating it here makes all downstream safety
        # calculations and broker parameters use the derived protection consistently.
        object.__setattr__(signal, "stop_loss", stop_loss)
        object.__setattr__(signal, "take_profits", (take_profit,))
        object.__setattr__(signal, "has_open_runner", False)
        return executable, initial_state

    resolve_entry._bare_gold_now_supported = True  # type: ignore[attr-defined]
    PaperExecutionPriorityService._resolve_entry = resolve_entry


def install_bare_gold_now_override() -> None:
    _install_v1_policy()
    _install_canonical_profile()
    _install_execution_profile()


__all__ = [
    "GOLD_PROVIDER_PIP",
    "PROFILE",
    "STOP_LOSS_PIPS",
    "TAKE_PROFIT_PIPS",
    "bare_now_side",
    "install_bare_gold_now_override",
]
