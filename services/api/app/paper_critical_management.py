"""Layer-aware provider management for the Owner Vantage DEMO paper boundary.

Ordinary Day-27 management remains unchanged. This subclass is selected only for
commands whose mechanically extracted target carries entry/layer/partial scope. It
never acts symbol-wide: every mutation uses a mapped broker position/order ID.

Partial semantics:
* if a selected entry layer still has multiple TP/runner tranches, "take partials"
  closes the nearest surviving TP tranche and leaves the others running;
* if only one tranche remains, a true MetaAPI POSITION_PARTIAL is used only when the
  broker's minimum/step rules allow both the closed and remaining volume;
* an impossible partial fails closed and never becomes a full close.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.metaapi_gateway import MetaApiGatewayError
from app.mt5_crypto import BrokerCredentialDecryptionError
from app.mt5_management_day27 import (
    Day27ManagementError,
    Day27ManagementResult,
    Day27Mt5ManagementService,
)
from app.paper_partial_close_gateway import PaperPartialCloseGateway


@dataclass(frozen=True, slots=True)
class _LayerPosition:
    id: UUID
    entry_index: int
    tp_index: int
    broker_position_id: str | None
    broker_order_id: str | None
    status: str
    stop_loss: Decimal | None
    take_profit: Decimal | None
    entry_price: Decimal | None
    volume: Decimal | None


class PaperCriticalManagementService(Day27Mt5ManagementService):
    """Apply explicit layer/partial commands to the connected DEMO account only."""

    async def execute_owner_demo_event(
        self,
        *,
        owner_user_id: UUID,
        lifecycle_event_id: UUID,
    ) -> Day27ManagementResult:
        event = self._load_event(lifecycle_event_id)
        if event is None:
            raise Day27ManagementError("day27_lifecycle_event_not_found")
        actions = self._actions(event)
        if not actions:
            raise Day27ManagementError("day27_management_action_missing")

        if not self._needs_critical_management(actions):
            return await super().execute_owner_demo_event(
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
            )

        existing = self._existing_success(owner_user_id, lifecycle_event_id)
        if existing is not None:
            return existing

        account = self._load_account(owner_user_id)
        if account is None:
            raise Day27ManagementError("mt5_account_not_configured")
        # _load_account in the parent already enforces account_environment='demo'.
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
        except BrokerCredentialDecryptionError as exc:
            raise Day27ManagementError("broker_credential_decryption_failed") from exc

        try:
            region = await self._read.resolve_account_region(
                token=token,
                account_id=account.account_id,
            )
        except MetaApiGatewayError as exc:
            raise Day27ManagementError(exc.code, retryable=exc.retryable) from exc

        signal_id = UUID(str(event["signal_id"]))
        side, symbol = self._signal_identity(signal_id)
        counters = {
            "broker_actions_sent": 0,
            "positions_closed": 0,
            "positions_modified": 0,
            "orders_cancelled": 0,
            "external_positions_reconciled": 0,
        }

        try:
            for action in actions:
                broker_positions = await self._broker_positions(
                    token=token,
                    account_id=account.account_id,
                    region=region,
                )
                counters["external_positions_reconciled"] += self._reconcile_missing_positions(
                    signal_id=signal_id,
                    user_id=owner_user_id,
                    broker_position_ids=set(broker_positions),
                )
                local_positions = self._load_layer_positions(signal_id, owner_user_id)
                open_positions = tuple(
                    item
                    for item in local_positions
                    if item.status == "open"
                    and item.broker_position_id is not None
                    and item.broker_position_id in broker_positions
                )
                pending_positions = tuple(
                    item
                    for item in local_positions
                    if item.status == "pending" and item.broker_order_id is not None
                )

                action_type = str(action.get("type") or "")
                target = str(action.get("target") or "all")
                value = self._positive_decimal(action.get("value"))

                if action_type == "close" and "partial" in target.lower():
                    await self._apply_partial(
                        token=token,
                        account_id=account.account_id,
                        region=region,
                        symbol=symbol,
                        target=target,
                        open_positions=open_positions,
                        broker_positions=broker_positions,
                        counters=counters,
                    )
                    continue

                if action_type == "close":
                    selected = self._select_layer_positions(
                        open_positions,
                        target,
                        side=side,
                    )
                    for item in selected:
                        assert item.broker_position_id is not None
                        await self._trade.close_position(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            position_id=item.broker_position_id,
                        )
                        counters["broker_actions_sent"] += 1
                        counters["positions_closed"] += 1
                        self._mark_provider_closed(item.id)

                    # A provider full-close must also cancel the exact unfilled
                    # layers for the same target. Otherwise the trade can reopen
                    # later with its old SL after users were told it was closed.
                    selected_pending = self._select_layer_positions(
                        pending_positions,
                        target,
                        side=side,
                    )
                    if selected_pending:
                        broker_orders = await self._broker_orders(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                        )
                        for item in selected_pending:
                            assert item.broker_order_id is not None
                            if item.broker_order_id not in broker_orders:
                                continue
                            await self._trade.cancel_order(
                                token=token,
                                account_id=account.account_id,
                                region=region,
                                order_id=item.broker_order_id,
                            )
                            counters["broker_actions_sent"] += 1
                            counters["orders_cancelled"] += 1
                            self._mark_pending_cancelled(
                                signal_id,
                                owner_user_id,
                                item.broker_order_id,
                            )
                    continue

                if action_type in {"move_to_break_even", "edit_stop_loss"}:
                    selected = self._select_layer_positions(
                        open_positions,
                        target,
                        side=side,
                    )
                    for item in selected:
                        assert item.broker_position_id is not None
                        broker = broker_positions[item.broker_position_id]
                        desired_sl = (
                            self._positive_decimal(broker.get("openPrice"))
                            if action_type == "move_to_break_even"
                            else value
                        )
                        if desired_sl is None:
                            raise Day27ManagementError("day27_stop_loss_value_invalid")
                        current_sl = self._positive_decimal(broker.get("stopLoss"))
                        if self._same_price(current_sl, desired_sl):
                            self._update_local_stop(item.id, desired_sl)
                            continue
                        await self._trade.modify_position(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            position_id=item.broker_position_id,
                            stop_loss=float(desired_sl),
                        )
                        counters["broker_actions_sent"] += 1
                        counters["positions_modified"] += 1
                        self._update_local_stop(item.id, desired_sl)

                    if action_type == "move_to_break_even":
                        # An unfilled order cannot be made risk-free at its own
                        # entry without risking broker rejection or an unsafe
                        # fill-to-modify race. Cancel the exact targeted pending
                        # tickets so the provider trade cannot create fresh risk
                        # after the risk-free instruction.
                        selected_pending = self._select_layer_positions(
                            pending_positions,
                            target,
                            side=side,
                        )
                        if selected_pending:
                            broker_orders = await self._broker_orders(
                                token=token,
                                account_id=account.account_id,
                                region=region,
                            )
                            for item in selected_pending:
                                assert item.broker_order_id is not None
                                if item.broker_order_id not in broker_orders:
                                    continue
                                await self._trade.cancel_order(
                                    token=token,
                                    account_id=account.account_id,
                                    region=region,
                                    order_id=item.broker_order_id,
                                )
                                counters["broker_actions_sent"] += 1
                                counters["orders_cancelled"] += 1
                                self._mark_pending_cancelled(
                                    signal_id,
                                    owner_user_id,
                                    item.broker_order_id,
                                )
                    continue

                if action_type == "edit_take_profit":
                    if value is None:
                        raise Day27ManagementError("day27_take_profit_value_invalid")
                    selected = self._select_layer_positions(
                        open_positions,
                        target,
                        side=side,
                    )
                    for item in selected:
                        assert item.broker_position_id is not None
                        broker = broker_positions[item.broker_position_id]
                        current_tp = self._positive_decimal(broker.get("takeProfit"))
                        if self._same_price(current_tp, value):
                            self._update_local_tp(item.id, value)
                            continue
                        await self._trade.modify_position(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            position_id=item.broker_position_id,
                            take_profit=float(value),
                        )
                        counters["broker_actions_sent"] += 1
                        counters["positions_modified"] += 1
                        self._update_local_tp(item.id, value)
                    continue

                if action_type == "cancel_pending":
                    broker_orders = await self._broker_orders(
                        token=token,
                        account_id=account.account_id,
                        region=region,
                    )
                    mapped_order_ids = {
                        item.broker_order_id
                        for item in local_positions
                        if item.broker_order_id is not None
                    }
                    for order_id in sorted(mapped_order_ids.intersection(broker_orders)):
                        await self._trade.cancel_order(
                            token=token,
                            account_id=account.account_id,
                            region=region,
                            order_id=order_id,
                        )
                        counters["broker_actions_sent"] += 1
                        counters["orders_cancelled"] += 1
                        self._mark_pending_cancelled(signal_id, owner_user_id, order_id)
                    continue

                raise Day27ManagementError("day27_management_action_unsupported")
        except MetaApiGatewayError as exc:
            self._audit_failure(
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
                signal_id=signal_id,
                actions=actions,
                counters=counters,
                error_code=exc.code,
            )
            raise Day27ManagementError(exc.code, retryable=exc.retryable) from exc
        except Day27ManagementError as exc:
            self._audit_failure(
                owner_user_id=owner_user_id,
                lifecycle_event_id=lifecycle_event_id,
                signal_id=signal_id,
                actions=actions,
                counters=counters,
                error_code=exc.code,
            )
            raise

        result = Day27ManagementResult(
            lifecycle_event_id=lifecycle_event_id,
            signal_id=signal_id,
            user_id=owner_user_id,
            actions_requested=len(actions),
            broker_actions_sent=counters["broker_actions_sent"],
            positions_closed=counters["positions_closed"],
            positions_modified=counters["positions_modified"],
            orders_cancelled=counters["orders_cancelled"],
            external_positions_reconciled=counters["external_positions_reconciled"],
        )
        self._audit_success(result, actions)
        return result

    @staticmethod
    def _needs_critical_management(actions: tuple[dict[str, Any], ...]) -> bool:
        return any(
            any(token in str(action.get("target") or "").lower() for token in ("entry_", "layer", "partial"))
            for action in actions
        )

    def _signal_identity(self, signal_id: UUID) -> tuple[str, str]:
        with self._session_factory() as session:
            row = session.execute(
                text("SELECT side, symbol FROM signals WHERE id=:id LIMIT 1"),
                {"id": signal_id},
            ).mappings().first()
        if row is None:
            raise Day27ManagementError("signal_not_found")
        side = str(row["side"] or "").upper()
        symbol = str(row["symbol"] or "").upper()
        if side not in {"BUY", "SELL"} or symbol != "XAUUSD":
            raise Day27ManagementError("critical_signal_identity_invalid")
        return side, symbol

    def _load_layer_positions(
        self,
        signal_id: UUID,
        user_id: UUID,
    ) -> tuple[_LayerPosition, ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, entry_index, tp_index, broker_position_id,
                           broker_order_id, status, stop_loss, take_profit,
                           entry_price, volume
                    FROM positions
                    WHERE signal_id=:signal_id AND user_id=:user_id
                    ORDER BY entry_index, tp_index
                    """
                ),
                {"signal_id": signal_id, "user_id": user_id},
            ).mappings().all()
        return tuple(
            _LayerPosition(
                id=UUID(str(row["id"])),
                entry_index=int(row["entry_index"]),
                tp_index=int(row["tp_index"]),
                broker_position_id=(
                    str(row["broker_position_id"])
                    if row["broker_position_id"]
                    else None
                ),
                broker_order_id=(
                    str(row["broker_order_id"])
                    if row["broker_order_id"]
                    else None
                ),
                status=str(row["status"]),
                stop_loss=self._positive_decimal(row["stop_loss"]),
                take_profit=self._positive_decimal(row["take_profit"]),
                entry_price=self._positive_decimal(row["entry_price"]),
                volume=self._positive_decimal(row["volume"]),
            )
            for row in rows
        )

    @classmethod
    def _select_layer_positions(
        cls,
        positions: tuple[_LayerPosition, ...],
        target: str,
        *,
        side: str,
    ) -> tuple[_LayerPosition, ...]:
        normalized = target.strip().lower()
        if normalized in {"", "all", "remaining", "rest"}:
            return positions

        entry_tp = cls._entry_tp_target(normalized)
        if entry_tp is not None:
            entry_index, tp_index = entry_tp
            return tuple(
                item
                for item in positions
                if item.entry_index == entry_index and item.tp_index == tp_index
            )

        if normalized.startswith("entry_") or normalized.startswith("entry"):
            digits = "".join(char for char in normalized.split("tp", 1)[0] if char.isdigit())
            if digits:
                index = int(digits)
                return tuple(item for item in positions if item.entry_index == index)

        if normalized.startswith("tp") and normalized[2:].isdigit():
            index = int(normalized[2:])
            return tuple(item for item in positions if item.tp_index == index)

        if normalized.startswith("position"):
            digits = "".join(char for char in normalized if char.isdigit())
            if digits:
                index = int(digits)
                return tuple(item for item in positions if item.tp_index == index)

        if normalized.startswith("first_") and normalized.endswith("_layers"):
            count = cls._target_count(normalized)
            entries = sorted({item.entry_index for item in positions})
            if count <= 0 or count > len(entries):
                raise Day27ManagementError("layer_target_count_unavailable")
            chosen = set(entries[:count])
            return tuple(item for item in positions if item.entry_index in chosen)

        if normalized.startswith("worst_") and normalized.endswith("_layers"):
            count = cls._target_count(normalized)
            groups: dict[int, list[_LayerPosition]] = {}
            for item in positions:
                groups.setdefault(item.entry_index, []).append(item)
            if count <= 0 or count >= len(groups):
                # ``worst_N_layers`` is emitted only for wording that explicitly says
                # leave/keep the best running. Never close every available layer.
                raise Day27ManagementError("layer_close_would_remove_best")
            representative: dict[int, Decimal] = {}
            for entry_index, items in groups.items():
                values = [item.entry_price for item in items if item.entry_price is not None]
                if not values:
                    raise Day27ManagementError("layer_entry_price_unavailable")
                representative[entry_index] = sum(values, Decimal("0")) / Decimal(len(values))
            reverse = side == "BUY"  # BUY worst is highest; SELL worst is lowest.
            ranked = sorted(representative, key=representative.get, reverse=reverse)
            chosen = set(ranked[:count])
            return tuple(item for item in positions if item.entry_index in chosen)

        raise Day27ManagementError("day27_management_target_unsupported")

    async def _apply_partial(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        symbol: str,
        target: str,
        open_positions: tuple[_LayerPosition, ...],
        broker_positions: dict[str, dict[str, object]],
        counters: dict[str, int],
    ) -> None:
        scoped = self._partial_scope(open_positions, target)
        if not scoped:
            return

        groups: dict[int, list[_LayerPosition]] = {}
        for item in scoped:
            groups.setdefault(item.entry_index, []).append(item)

        specification: dict[str, object] | None = None
        partial_gateway = PaperPartialCloseGateway(self._trade)
        for items in groups.values():
            ordered = sorted(items, key=lambda item: item.tp_index)
            if len(ordered) > 1:
                # One complete TP/runner tranche is the system's deterministic partial.
                item = ordered[0]
                assert item.broker_position_id is not None
                await self._trade.close_position(
                    token=token,
                    account_id=account_id,
                    region=region,
                    position_id=item.broker_position_id,
                )
                counters["broker_actions_sent"] += 1
                counters["positions_closed"] += 1
                self._mark_provider_closed(item.id)
                continue

            item = ordered[0]
            assert item.broker_position_id is not None
            broker = broker_positions[item.broker_position_id]
            current_volume = self._positive_decimal(broker.get("volume"))
            if current_volume is None:
                raise Day27ManagementError("partial_broker_volume_invalid")
            if specification is None:
                specification = await self._read.read_symbol_specification(
                    token=token,
                    account_id=account_id,
                    region=region,
                    symbol=symbol,
                )
            close_volume, remaining_volume = self._partial_volumes(
                current=current_volume,
                minimum=self._required_positive_decimal(
                    specification.get("minVolume"),
                    "partial_min_volume_invalid",
                ),
                step=self._required_positive_decimal(
                    specification.get("volumeStep"),
                    "partial_volume_step_invalid",
                ),
            )
            await partial_gateway.close_partial(
                account_environment="demo",
                token=token,
                account_id=account_id,
                region=region,
                position_id=item.broker_position_id,
                volume=float(close_volume),
            )
            counters["broker_actions_sent"] += 1
            counters["positions_modified"] += 1
            self._update_local_volume(item.id, remaining_volume)

    @staticmethod
    def _partial_scope(
        positions: tuple[_LayerPosition, ...],
        target: str,
    ) -> tuple[_LayerPosition, ...]:
        normalized = target.strip().lower()
        if normalized == "partial_tp1":
            # Partial means reduce current exposure, not "close TP1 even if TP1 has
            # already settled". Scope all open tranches, then the caller takes the
            # nearest surviving tranche per entry layer.
            return positions
        if normalized.startswith("entry_") and "_partial_tp1" in normalized:
            prefix = normalized.split("_partial_tp1", 1)[0]
            digits = "".join(char for char in prefix if char.isdigit())
            if digits:
                index = int(digits)
                return tuple(item for item in positions if item.entry_index == index)
        raise Day27ManagementError("partial_target_unsupported")

    @staticmethod
    def _partial_volumes(
        *,
        current: Decimal,
        minimum: Decimal,
        step: Decimal,
    ) -> tuple[Decimal, Decimal]:
        if current <= 0 or minimum <= 0 or step <= 0:
            raise Day27ManagementError("partial_volume_rules_invalid")
        if current < minimum * Decimal("2"):
            raise Day27ManagementError("partial_volume_below_broker_minimum")
        desired = current / Decimal("2")
        close_steps = (desired / step).to_integral_value(rounding=ROUND_FLOOR)
        close_volume = close_steps * step
        if close_volume < minimum:
            close_volume = minimum
        remaining = current - close_volume
        if close_volume < minimum or remaining < minimum or close_volume <= 0 or remaining <= 0:
            raise Day27ManagementError("partial_volume_below_broker_minimum")
        # Both legs must lie on the broker step. If current itself is broker-valid,
        # this also guarantees the remainder is valid after closing close_volume.
        if (close_volume / step) % 1 != 0 or (remaining / step) % 1 != 0:
            raise Day27ManagementError("partial_volume_step_invalid")
        return close_volume, remaining

    @staticmethod
    def _entry_tp_target(normalized: str) -> tuple[int, int] | None:
        if not normalized.startswith("entry_") or "_tp" not in normalized:
            return None
        left, right = normalized.split("_tp", 1)
        entry_digits = "".join(char for char in left if char.isdigit())
        tp_digits = "".join(char for char in right if char.isdigit())
        if entry_digits and tp_digits:
            return int(entry_digits), int(tp_digits)
        return None

    @staticmethod
    def _target_count(normalized: str) -> int:
        digits = "".join(char for char in normalized if char.isdigit())
        return int(digits) if digits else 0

    @staticmethod
    def _required_positive_decimal(value: object, code: str) -> Decimal:
        parsed = PaperCriticalManagementService._positive_decimal(value)
        if parsed is None:
            raise Day27ManagementError(code)
        return parsed

    def _update_local_volume(self, position_id: UUID, value: Decimal) -> None:
        with self._session_factory() as session:
            session.execute(
                text("UPDATE positions SET volume=:volume, updated_at=now() WHERE id=:id"),
                {"id": position_id, "volume": value},
            )
            session.commit()

    def _mark_pending_cancelled(
        self,
        signal_id: UUID,
        user_id: UUID,
        order_id: str,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='closed', closed_at=COALESCE(closed_at,:now),
                        close_reason=COALESCE(close_reason,'provider_cancel'),
                        updated_at=:now
                    WHERE signal_id=:signal_id AND user_id=:user_id
                      AND broker_order_id=:order_id AND status='pending'
                    """
                ),
                {
                    "signal_id": signal_id,
                    "user_id": user_id,
                    "order_id": order_id,
                    "now": now,
                },
            )
            session.commit()


__all__ = ["PaperCriticalManagementService"]
