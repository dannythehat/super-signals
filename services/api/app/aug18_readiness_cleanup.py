"""Final readiness fixes found during the complete 18 Aug production audit.

These corrections preserve broker truth and the shared paper/future-LIVE engine:

* Telegram publication formatting is total. Sparse canonical rows render missing
  display-only values as ``N/A``; nothing is invented.
* ``broker_deals`` remains append-only: account truth inserts unseen deals and ignores
  duplicates, never updating immutable broker history.
* A provider numeric risk-free stop remains authoritative across modest broker-fill
  slippage. Large contradictions still fail closed as likely lifecycle mis-linkage.
* Full-history backfill audit binds use explicit PostgreSQL types.
* Recovered management is temporally scoped: an old provider update may only mutate
  positions/orders that already existed at that lifecycle event's provider timestamp.
  A pending layer that fills later cannot be closed by replay of an older instruction.
* Descriptive wording such as ``BEST ENTRY STILL RUNNING`` is not converted into a
  command to ``close all but best``. Explicit close prices in that same provider edit
  remain literal close instructions.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.mt5_management_day27 import Day27ManagementError

_installed = False
_RISK_FREE_PREFIX = "best_entry_risk_free_"
_RISK_FREE_FILL_TOLERANCE = Decimal("1.00")
_BEST_STILL_RUNNING = re.compile(r"\bBEST\s+ENTRY\s+STILL\s+RUNNING\b", re.IGNORECASE)
_MANAGEMENT_EVENT_CUTOFF: ContextVar[Any | None] = ContextVar(
    "super_signals_management_event_cutoff",
    default=None,
)


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
        return format(decimal_value.normalize(), "f")

    decimal_text._sparse_safe = True  # type: ignore[attr-defined]
    publisher._decimal_text = decimal_text


def _install_descriptive_best_entry_parser_fix() -> None:
    """Filter descriptive provider state only at the final executable policy boundary."""
    import app.v1_message_policy as v1_policy

    # Keep the low-level parser's historical contract intact. The final V1 policy is
    # where semantic text becomes broker actions, so that is the correct place to
    # remove an invented close without changing parser-only callers/tests.
    current = v1_policy.augment_management_actions
    if getattr(current, "_best_still_running_is_descriptive", False):
        return
    original = current

    def augment_management_actions(raw_text: str, actions):
        result = original(raw_text, actions)
        if _BEST_STILL_RUNNING.search(raw_text or "") is None:
            return result
        # "Best entry still running" reports state; it does not ask us to close a
        # different layer. Keep explicit entry-price closes and stop edits untouched.
        return tuple(
            action
            for action in result
            if not (
                str(action.get("type") or "") == "close"
                and str(action.get("target") or "").lower() == "all_but_best"
            )
        )

    augment_management_actions._best_still_running_is_descriptive = True  # type: ignore[attr-defined]
    v1_policy.augment_management_actions = augment_management_actions


def _install_literal_provider_risk_free_stop() -> None:
    from app.paper_critical_management_v2 import PaperCriticalManagementV2

    current = PaperCriticalManagementV2._select_layer_positions
    if getattr(current, "_literal_provider_stop", False):
        return
    original = current.__func__

    def select_layer_positions(cls, positions, target: str, *, side: str):
        normalized = target.strip().lower()
        if normalized.startswith(_RISK_FREE_PREFIX):
            wanted = cls._target_price(
                normalized.removeprefix(_RISK_FREE_PREFIX),
                "risk_free_stop_invalid",
            )
            groups, representative = cls._entry_groups(positions)
            if not groups:
                return ()
            best_index = cls._best_entry_index(representative, side=side)
            best_fill = representative[best_index]
            normalized_side = side.strip().upper()
            if normalized_side not in {"BUY", "SELL"}:
                raise Day27ManagementError("trade_side_invalid")
            protective = wanted >= best_fill if normalized_side == "BUY" else wanted <= best_fill
            if not protective and abs(wanted - best_fill) > _RISK_FREE_FILL_TOLERANCE:
                raise Day27ManagementError("risk_free_stop_not_protective")
            return tuple(groups[best_index])
        return original(cls, positions, target, side=side)

    select_layer_positions._literal_provider_stop = True  # type: ignore[attr-defined]
    PaperCriticalManagementV2._select_layer_positions = classmethod(select_layer_positions)


def _install_temporal_management_scope() -> None:
    from app.paper_critical_management import PaperCriticalManagementService, _LayerPosition

    current_execute = PaperCriticalManagementService.execute_owner_demo_event
    if getattr(current_execute, "_event_time_scoped", False):
        return
    original_execute = current_execute
    original_load_positions = PaperCriticalManagementService._load_layer_positions

    async def execute_owner_demo_event(self, *, owner_user_id: UUID, lifecycle_event_id: UUID):
        with self._session_factory() as session:
            cutoff = session.execute(
                text(
                    """
                    SELECT occurred_at
                    FROM signal_lifecycle_events
                    WHERE id=:event_id
                    LIMIT 1
                    """
                ),
                {"event_id": lifecycle_event_id},
            ).scalar_one_or_none()
        token = _MANAGEMENT_EVENT_CUTOFF.set(cutoff)
        try:
            return await original_execute(
                self,
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
            )
        finally:
            _MANAGEMENT_EVENT_CUTOFF.reset(token)

    def load_layer_positions(self, signal_id: UUID, user_id: UUID):
        cutoff = _MANAGEMENT_EVENT_CUTOFF.get()
        if cutoff is None:
            return original_load_positions(self, signal_id, user_id)
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, entry_index, tp_index, broker_position_id,
                           broker_order_id, status, stop_loss, take_profit,
                           entry_price, volume
                    FROM positions
                    WHERE signal_id=:signal_id
                      AND user_id=:user_id
                      AND COALESCE(opened_at, created_at) <= :cutoff
                    ORDER BY entry_index, tp_index
                    """
                ),
                {"signal_id": signal_id, "user_id": user_id, "cutoff": cutoff},
            ).mappings().all()
        return tuple(
            _LayerPosition(
                id=UUID(str(row["id"])),
                entry_index=int(row["entry_index"]),
                tp_index=int(row["tp_index"]),
                broker_position_id=(str(row["broker_position_id"]) if row["broker_position_id"] else None),
                broker_order_id=(str(row["broker_order_id"]) if row["broker_order_id"] else None),
                status=str(row["status"]),
                stop_loss=self._positive_decimal(row["stop_loss"]),
                take_profit=self._positive_decimal(row["take_profit"]),
                entry_price=self._positive_decimal(row["entry_price"]),
                volume=self._positive_decimal(row["volume"]),
            )
            for row in rows
        )

    execute_owner_demo_event._event_time_scoped = True  # type: ignore[attr-defined]
    load_layer_positions._event_time_scoped = True  # type: ignore[attr-defined]
    PaperCriticalManagementService.execute_owner_demo_event = execute_owner_demo_event
    PaperCriticalManagementService._load_layer_positions = load_layer_positions


def _install_typed_full_backfill_marker() -> None:
    import app.performance_account_truth_override as account_truth

    original = account_truth._mark_full_backfill
    if getattr(original, "_explicit_postgres_types", False):
        return

    def mark_full_backfill(
        self: Any,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        start_time,
        end_time,
        deal_count: int,
    ) -> None:
        payload = json.dumps(
            {
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "broker_deals_seen": deal_count,
                "trade_action_created": False,
            },
            separators=(",", ":"),
        )
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        actor_user_id,event_type,entity_type,entity_id,payload
                    )
                    SELECT
                        CAST(:user_id AS uuid),CAST(:event_type AS varchar),
                        CAST('mt5_account' AS varchar),CAST(:account_id AS uuid),
                        CAST(:payload AS jsonb)
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM audit_events
                        WHERE actor_user_id=CAST(:user_id AS uuid)
                          AND event_type=CAST(:event_type AS varchar)
                          AND entity_type=CAST('mt5_account' AS varchar)
                          AND entity_id=CAST(:account_id AS uuid)
                    )
                    """
                ),
                {
                    "user_id": user_id,
                    "event_type": account_truth._CHECKPOINT_EVENT,
                    "account_id": mt5_account_id,
                    "payload": payload,
                },
            )
            session.commit()

    mark_full_backfill._explicit_postgres_types = True  # type: ignore[attr-defined]
    account_truth._mark_full_backfill = mark_full_backfill


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
                        "volume": account_truth._d(payload.get("volume")) if payload.get("volume") is not None else None,
                        "price": account_truth._d(payload.get("price")) if payload.get("price") is not None else None,
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
    _install_descriptive_best_entry_parser_fix()
    _install_literal_provider_risk_free_stop()
    _install_temporal_management_scope()
    _install_typed_full_backfill_marker()
    _install_append_only_broker_deal_sync()
    _installed = True


__all__ = ["install_aug18_readiness_cleanup"]
