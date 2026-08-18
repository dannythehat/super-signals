"""Narrow production corrections for provider entry dialects observed in paper testing.

This module deliberately handles only mechanically explicit forms seen in production:
* the TDC plural two-price pending form with TP OPEN;
* present-tense numeric entries such as "I'm selling 4390" are never chatter;
* linked "OPEN EXTRA GOLD SELLS/BUYS" duplicates the currently-open protected
  tranches as one new paper-only entry layer.

No absent SL/TP is invented. The existing BUY/SELL GOLD NOW fallback remains a
separate rule and is not widened here.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.critical_entry_policy import CriticalEntry
from app.day27_management_policy import Day27ManagementPolicyResult
from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_management_day27 import Day27ManagementError, Day27ManagementResult

_TWO_POINT_PENDING = re.compile(
    r"(?im)^\s*(BUY|SELL)\s+(LIMITS?|STOPS?)\s+(?:XAUUSD|GOLD)\s*"
    r"@\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)(?:\s+AREA)?\s*$"
)
_PRESENT_TENSE_ENTRY = re.compile(
    r"\b(?:I\s*['’]?M|I\s+AM)\s+(BUYING|SELLING)\s+"
    r"(?:(?:GOLD|XAUUSD)\s+)?(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
# The explicit instruction is the first line. Providers may append emotional/hype text
# on later lines; that must not erase the command.
_OPEN_EXTRA = re.compile(
    r"(?im)^\s*OPEN\s+EXTRA\s+(?:GOLD|XAUUSD)\s+(BUYS?|SELLS?)\b"
)
_TP_OPEN = re.compile(r"(?im)^\s*TP\s+OPEN\s*$")

_installed = False


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _install_two_point_pending() -> None:
    import app.critical_entry_policy as policy
    import app.paper_critical_execution as execution

    original = policy.parse_critical_entries
    if getattr(original, "_provider_two_point_pending", False):
        return

    def wrapped(raw_text: str, *, side: str, entry_low: object, entry_high: object):
        try:
            return original(
                raw_text,
                side=side,
                entry_low=entry_low,
                entry_high=entry_high,
            )
        except ValueError as exc:
            if str(exc) != "pending_layer_grid_unspecified":
                raise

        text_value = raw_text or ""
        match = _TWO_POINT_PENDING.search(text_value)
        # This exception is deliberately narrower than the generic plural-zone grammar.
        # The exact observed TDC form includes TP OPEN. Unknown plural zones continue to
        # fail closed instead of having a grid or two-point plan invented for them.
        if (
            match is None
            or _TP_OPEN.search(text_value) is None
            or re.search(r"\bSL\b", text_value, re.IGNORECASE) is None
            or re.search(r"\bTP\b", text_value, re.IGNORECASE) is None
        ):
            raise ValueError("pending_layer_grid_unspecified")

        requested_side = match.group(1).upper()
        normalized_side = side.strip().upper()
        if requested_side != normalized_side:
            raise ValueError("pending_side_mismatch")
        first = _decimal(match.group(3))
        second = _decimal(match.group(4))
        if first is None or second is None or first == second:
            raise ValueError("pending_entry_invalid")
        kind = match.group(2).upper()
        suffix = "limit" if kind.startswith("LIMIT") else "stop"
        order_type = f"{normalized_side.lower()}_{suffix}"
        return (
            CriticalEntry(1, order_type, first),
            CriticalEntry(2, order_type, second),
        )

    wrapped._provider_two_point_pending = True  # type: ignore[attr-defined]
    policy.parse_critical_entries = wrapped
    execution.parse_critical_entries = wrapped


def _install_present_tense_classifier() -> None:
    import app.message_classifier as classifier

    original = classifier.classify_message
    if getattr(original, "_provider_present_tense_entry", False):
        return

    def wrapped(raw_text: str, *, reply_to_message_id: int | None = None):
        result = original(raw_text, reply_to_message_id=reply_to_message_id)
        if result.classification == "chatter" and _PRESENT_TENSE_ENTRY.search(raw_text or ""):
            return classifier.ClassificationResult(
                classification="uncertain",
                decision_status="review",
                reason="Present-tense numeric BUY/SELL entry is trade-like and must reach semantic interpretation, not chatter.",
                matched_rules=("present_tense_numeric_entry",),
            )
        return result

    wrapped._provider_present_tense_entry = True  # type: ignore[attr-defined]
    classifier.classify_message = wrapped


def _install_extra_entry_management() -> None:
    import app.day27_management_policy as day27
    import app.v1_message_policy as v1
    from app.mt5_management_day27 import Day27Mt5ManagementService

    original_extract = day27.extract_day27_management_actions
    if not getattr(original_extract, "_provider_extra_entry", False):
        def wrapped_extract(raw_text: str):
            existing = original_extract(raw_text)
            match = _OPEN_EXTRA.search(raw_text or "")
            if match is None:
                return existing
            word = match.group(1).upper()
            side = "BUY" if word.startswith("BUY") else "SELL"
            actions = [dict(action) for action in existing.actions]
            action = {"type": "add_market", "target": "same_trade", "value": side}
            if action not in actions:
                actions.append(action)
            return Day27ManagementPolicyResult(
                tuple(actions),
                "explicit_active_trade_add_entry",
            )

        wrapped_extract._provider_extra_entry = True  # type: ignore[attr-defined]
        day27.extract_day27_management_actions = wrapped_extract
        v1.extract_day27_management_actions = wrapped_extract

    original_execute = Day27Mt5ManagementService.execute_owner_demo_event
    if getattr(original_execute, "_provider_extra_entry", False):
        return

    async def wrapped_execute(self, *, owner_user_id: UUID, lifecycle_event_id: UUID):
        event = self._load_event(lifecycle_event_id)
        if event is None:
            return await original_execute(
                self,
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
            )
        actions = self._actions(event)
        add_actions = [a for a in actions if str(a.get("type") or "") == "add_market"]
        if not add_actions:
            return await original_execute(
                self,
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
            )
        if len(actions) != 1 or len(add_actions) != 1:
            raise Day27ManagementError("day27_add_market_compound_unsupported")
        existing = self._existing_success(owner_user_id, lifecycle_event_id)
        if existing is not None:
            return existing

        requested_side = str(add_actions[0].get("value") or "").upper()
        if requested_side not in {"BUY", "SELL"}:
            raise Day27ManagementError("day27_add_market_side_invalid")
        signal_id = UUID(str(event["signal_id"]))
        account = self._load_account(owner_user_id)
        if account is None:
            raise Day27ManagementError("mt5_account_not_configured")
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
            region = await self._read.resolve_account_region(
                token=token,
                account_id=account.account_id,
            )
        except Exception as exc:
            code = getattr(exc, "code", "broker_credential_decryption_failed")
            raise Day27ManagementError(
                str(code), retryable=bool(getattr(exc, "retryable", False))
            ) from exc

        with self._session_factory() as session:
            signal = session.execute(
                text("SELECT symbol, side FROM signals WHERE id=:signal_id LIMIT 1"),
                {"signal_id": signal_id},
            ).mappings().first()
            rows = session.execute(
                text(
                    """
                    SELECT id, entry_index, tp_index, planned_risk_percent, volume,
                           stop_loss, take_profit, broker_position_id
                    FROM positions
                    WHERE signal_id=:signal_id AND user_id=:user_id AND status='open'
                    ORDER BY entry_index, tp_index
                    """
                ),
                {"signal_id": signal_id, "user_id": owner_user_id},
            ).mappings().all()
            max_entry = int(
                session.execute(
                    text(
                        "SELECT COALESCE(MAX(entry_index),0) FROM positions "
                        "WHERE signal_id=:signal_id AND user_id=:user_id"
                    ),
                    {"signal_id": signal_id, "user_id": owner_user_id},
                ).scalar_one()
            )
        if signal is None:
            raise Day27ManagementError("signal_not_found")
        symbol = str(signal["symbol"] or "").upper()
        side = str(signal["side"] or "").upper()
        if symbol != "XAUUSD" or side != requested_side:
            raise Day27ManagementError("day27_add_market_signal_mismatch")
        if not rows:
            raise Day27ManagementError("day27_add_market_no_open_positions")

        broker_positions = await self._broker_positions(
            token=token,
            account_id=account.account_id,
            region=region,
        )
        new_entry_index = max_entry + 1
        planned: list[dict[str, Any]] = []
        for row in rows:
            broker_id = str(row["broker_position_id"] or "")
            broker = broker_positions.get(broker_id)
            if broker is None:
                continue
            volume = _decimal(row["volume"])
            planned_risk = _decimal(row["planned_risk_percent"])
            sl = _decimal(broker.get("stopLoss"))
            broker_tp = _decimal(broker.get("takeProfit"))
            local_tp = _decimal(row["take_profit"])
            if volume is None or planned_risk is None or sl is None:
                raise Day27ManagementError("day27_add_market_protection_invalid")
            if local_tp is not None and broker_tp is None:
                raise Day27ManagementError("day27_add_market_broker_tp_missing")
            tp_index = int(row["tp_index"])
            client_id = f"SSX_{signal_id.hex[:8]}_{new_entry_index}{tp_index}"
            planned.append(
                {
                    "tp_index": tp_index,
                    "planned_risk_percent": planned_risk,
                    "volume": volume,
                    "stop_loss": sl,
                    "take_profit": broker_tp,
                    "client_id": client_id,
                }
            )
        if not planned:
            raise Day27ManagementError("day27_add_market_no_broker_positions")

        created: list[dict[str, Any]] = []
        try:
            for item in planned:
                current_positions = await self._broker_positions(
                    token=token,
                    account_id=account.account_id,
                    region=region,
                )
                already = next(
                    (
                        p for p in current_positions.values()
                        if str(p.get("clientId") or "") == item["client_id"]
                    ),
                    None,
                )
                if already is not None:
                    position_id = str(already.get("id") or "")
                    order_id = str(already.get("orderId") or position_id)
                else:
                    result = await self._trade.place_market_order(
                        token=token,
                        account_id=account.account_id,
                        region=region,
                        side=side,
                        symbol=symbol,
                        volume=float(item["volume"]),
                        stop_loss=float(item["stop_loss"]),
                        take_profit=(
                            float(item["take_profit"])
                            if item["take_profit"] is not None
                            else None
                        ),
                        client_id=item["client_id"],
                    )
                    order_id = result.order_id
                    position_id = str(result.position_id or "")
                    if not position_id:
                        refreshed = await self._broker_positions(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                        )
                        matched = next(
                            (
                                p for p in refreshed.values()
                                if str(p.get("clientId") or "") == item["client_id"]
                            ),
                            None,
                        )
                        position_id = str((matched or {}).get("id") or "")
                if not position_id:
                    raise Day27ManagementError("day27_add_market_position_unresolved")
                created.append({**item, "order_id": order_id, "position_id": position_id})
        except (MetaApiGatewayError, Day27ManagementError) as exc:
            for item in reversed(created):
                try:
                    await self._trade.close_position(
                        token=token,
                        account_id=account.account_id,
                        region=region,
                        position_id=item["position_id"],
                    )
                except Exception:
                    pass
            if isinstance(exc, Day27ManagementError):
                raise
            raise Day27ManagementError(exc.code, retryable=exc.retryable) from exc

        refreshed = await self._broker_positions(
            token=token,
            account_id=account.account_id,
            region=region,
        )
        now = datetime.now(UTC)
        with self._session_factory() as session:
            for item in created:
                broker = refreshed.get(item["position_id"], {})
                entry_price = _decimal(broker.get("openPrice"))
                if entry_price is None:
                    raise Day27ManagementError("day27_add_market_entry_price_missing")
                session.execute(
                    text(
                        """
                        INSERT INTO positions (
                            signal_id, user_id, entry_index, tp_index,
                            entry_order_type, take_profit, planned_risk_percent,
                            volume, stop_loss, broker_order_id, broker_position_id,
                            broker_client_id, status, entry_price, opened_at
                        ) VALUES (
                            :signal_id, :user_id, :entry_index, :tp_index,
                            'market', :take_profit, :planned_risk_percent,
                            :volume, :stop_loss, :broker_order_id, :broker_position_id,
                            :broker_client_id, 'open', :entry_price, :opened_at
                        )
                        ON CONFLICT (signal_id, user_id, entry_index, tp_index) DO NOTHING
                        """
                    ),
                    {
                        "signal_id": signal_id,
                        "user_id": owner_user_id,
                        "entry_index": new_entry_index,
                        "tp_index": item["tp_index"],
                        "take_profit": item["take_profit"],
                        "planned_risk_percent": item["planned_risk_percent"],
                        "volume": item["volume"],
                        "stop_loss": item["stop_loss"],
                        "broker_order_id": item["order_id"],
                        "broker_position_id": item["position_id"],
                        "broker_client_id": item["client_id"],
                        "entry_price": entry_price,
                        "opened_at": now,
                    },
                )
            session.commit()

        result = Day27ManagementResult(
            lifecycle_event_id=lifecycle_event_id,
            signal_id=signal_id,
            user_id=owner_user_id,
            actions_requested=1,
            broker_actions_sent=len(created),
            positions_closed=0,
            positions_modified=0,
            orders_cancelled=0,
            external_positions_reconciled=0,
        )
        self._audit_success(result, actions)
        return result

    wrapped_execute._provider_extra_entry = True  # type: ignore[attr-defined]
    Day27Mt5ManagementService.execute_owner_demo_event = wrapped_execute


def install_provider_entry_reliability_overrides() -> None:
    global _installed
    if _installed:
        return
    _install_two_point_pending()
    _install_present_tense_classifier()
    _install_extra_entry_management()
    _installed = True


__all__ = ["install_provider_entry_reliability_overrides"]
