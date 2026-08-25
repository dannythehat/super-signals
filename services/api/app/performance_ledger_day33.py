"""Day 33 canonical broker-backed performance ledger and signal timeline.

Raw broker deals are immutable. Trade outcomes and summaries are derived and may be
regenerated at any time from the raw deal ledger plus canonical Super Signals data.
The service has read-only broker capabilities only; it cannot place, close or modify
orders or positions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

MONEY = Decimal("0.01")
PCT = Decimal("0.000001")
XAUUSD_PIP_SIZE = Decimal("0.1")
MODEL_BALANCE = Decimal("500")
MODEL_NORMAL_RISK_PERCENT = Decimal("1")


class Day33LedgerError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Day33SyncResult:
    user_id: UUID
    broker_deals_added: int
    mapped_positions_checked: int
    outcomes_rebuilt: int
    summaries_rebuilt: int
    broker_trade_action_created: bool = False


@dataclass(frozen=True, slots=True)
class Day33PerformanceWindow:
    key: str
    label: str
    cash_pnl: Decimal
    return_percent: Decimal | None
    model_500_pnl: Decimal
    model_500_return_percent: Decimal
    closed_trades: int
    wins: int
    losses: int
    breakeven: int
    open_trades: int
    win_rate_percent: Decimal | None
    net_pips: Decimal | None
    mixed_instrument_pips: bool


@dataclass(frozen=True, slots=True)
class Day33TimelineTrade:
    signal_id: UUID
    symbol: str
    side: str
    status: str
    status_label: str
    status_color: str
    source_label: str | None
    trader_stream: str | None
    source_color_index: int | None
    opened_at: datetime | None
    closed_at: datetime | None
    position_count: int
    open_positions: int
    pending_positions: int
    closed_positions: int
    cash_pnl: Decimal | None
    net_pips: Decimal | None
    model_500_pnl: Decimal | None
    close_reason: str | None


@dataclass(frozen=True, slots=True)
class Day33TimelineView:
    trades: tuple[Day33TimelineTrade, ...]
    open_count: int
    pending_count: int
    provider_identity_visible: bool
    broker_trade_action_created: bool = False


def _d(value: object | None, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    return Decimal(str(value))


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _pct(value: Decimal) -> Decimal:
    return value.quantize(PCT, rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise Day33LedgerError("broker_deal_time_invalid")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        return _utc(datetime.fromisoformat(raw))
    except ValueError as exc:
        raise Day33LedgerError("broker_deal_time_invalid") from exc


def trader_stream_for(source_alias: str, original_text: str) -> str | None:
    """Return only attribution that is currently reliable; never guess."""
    alias = " ".join(source_alias.lower().split())
    content = " ".join(original_text.upper().split())
    if "matthew" in alias:
        return "Matthew"
    if "jeff" in alias:
        return "Jeff"
    if "tdc" in alias:
        # The standalone Matthew source proves this specific format lineage.
        if content.startswith("BUY LIMITS GOLD @") or content.startswith("SELL LIMITS GOLD @"):
            return "Matthew"
    return None


def stable_color_index(source_id: UUID, trader_stream: str | None) -> int:
    key = f"{source_id}:{trader_stream or ''}".encode("utf-8")
    return int.from_bytes(sha256(key).digest()[:2], "big") % 8


class Day33PerformanceLedgerService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        gateway: MetaApiReadGateway,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._gateway = gateway

    async def sync_user(self, user_id: UUID) -> Day33SyncResult:
        account = self._account(user_id)
        if account is None:
            raise Day33LedgerError("mt5_account_not_configured")
        if str(account["status"]) != "connected":
            raise Day33LedgerError("mt5_account_not_connected", retryable=True)
        try:
            token = self._cipher.decrypt(bytes(account["metaapi_token_ciphertext"]))
        except BrokerCredentialDecryptionError as exc:
            raise Day33LedgerError("broker_credential_decryption_failed") from exc

        account_id = str(account["metaapi_account_id"])
        try:
            region = await self._gateway.resolve_account_region(token=token, account_id=account_id)
            account_payload = await self._gateway.read_account_information(
                token=token, account_id=account_id, region=region
            )
        except MetaApiGatewayError as exc:
            raise Day33LedgerError(exc.code, retryable=exc.retryable) from exc

        captured_at = datetime.now(UTC)
        self._store_snapshot(
            user_id=user_id,
            mt5_account_id=account["id"],
            payload=account_payload,
            captured_at=captured_at,
        )

        positions = self._mapped_positions(user_id)
        deals_added = 0
        for row in positions:
            broker_position_id = row["broker_position_id"]
            if not broker_position_id:
                continue
            try:
                payloads = await self._gateway.read_deals_by_position(
                    token=token,
                    account_id=account_id,
                    region=region,
                    position_id=str(broker_position_id),
                )
            except MetaApiGatewayError as exc:
                if exc.retryable:
                    raise Day33LedgerError(exc.code, retryable=True) from exc
                # A missing old ticket must not fabricate a result. Keep the
                # mapped trade as closed_unknown/open and continue.
                payloads = []
            deals_added += self._store_deals(
                user_id=user_id,
                mt5_account_id=account["id"],
                position_row=row,
                payloads=payloads,
            )

        outcomes = self.rebuild_outcomes(user_id)
        summaries = self.rebuild_summaries(user_id)
        return Day33SyncResult(
            user_id=user_id,
            broker_deals_added=deals_added,
            mapped_positions_checked=len(positions),
            outcomes_rebuilt=outcomes,
            summaries_rebuilt=summaries,
        )

    def read_windows(self, user_id: UUID, *, now: datetime | None = None) -> tuple[Day33PerformanceWindow, ...]:
        point = _utc(now or datetime.now(UTC))
        start_today = point.replace(hour=0, minute=0, second=0, microsecond=0)
        start_month = start_today.replace(day=1)
        windows = (
            ("today", "Today", start_today),
            ("7d", "7 days", point - timedelta(days=7)),
            ("30d", "30 days", point - timedelta(days=30)),
            ("month", "Month", start_month),
            ("all", "All time", None),
        )
        return tuple(self._window(user_id, key, label, since, point) for key, label, since in windows)

    def read_timeline(
        self,
        user_id: UUID,
        *,
        viewer_role: str,
        status_filter: str | None = None,
        source_filter: UUID | None = None,
        trader_filter: str | None = None,
        limit: int = 100,
    ) -> Day33TimelineView:
        provider_visible = viewer_role in {"owner", "trading_admin"}
        rows = self._timeline_rows(user_id)
        trades: list[Day33TimelineTrade] = []
        for row in rows:
            trade = self._timeline_trade(row, provider_visible=provider_visible)
            if status_filter and status_filter != "all":
                normalized = status_filter.lower()
                accepted = {
                    "open": {"open"},
                    "pending": {"pending"},
                    "closed": {"won", "lost", "breakeven", "closed_unknown"},
                    "won": {"won"},
                    "lost": {"lost"},
                    "breakeven": {"breakeven"},
                }.get(normalized, {normalized})
                if trade.status not in accepted:
                    continue
            if provider_visible and source_filter and row["source_id"] != source_filter:
                continue
            if provider_visible and trader_filter and (trade.trader_stream or "") != trader_filter:
                continue
            trades.append(trade)
            if len(trades) >= max(1, min(limit, 250)):
                break

        return Day33TimelineView(
            trades=tuple(trades),
            open_count=sum(1 for item in trades if item.status == "open"),
            pending_count=sum(1 for item in trades if item.status == "pending"),
            provider_identity_visible=provider_visible,
        )

    def rebuild_outcomes(self, user_id: UUID) -> int:
        rows = self._mapped_positions(user_id)
        rebuilt = 0
        with self._session_factory() as session:
            for row in rows:
                deal_rows = session.execute(
                    text(
                        """
                        SELECT broker_deal_id, deal_type, entry_type, symbol, volume, price,
                               profit, commission, swap, occurred_at
                        FROM broker_deals
                        WHERE position_id=:position_id
                        ORDER BY occurred_at, broker_deal_id
                        """
                    ),
                    {"position_id": row["id"]},
                ).mappings().all()
                outcome = self._derive_outcome(session, row, deal_rows)
                session.execute(
                    text(
                        """
                        INSERT INTO performance_trade_outcomes (
                            position_id,user_id,signal_id,source_id,trader_stream,symbol,side,status,
                            opened_at,closed_at,entry_price,exit_price,volume,cash_pnl,return_percent,
                            net_pips,pip_size,model_500_pnl,model_500_return_percent,
                            planned_risk_percent,close_reason,broker_deal_count,source_digest,derived_at
                        ) VALUES (
                            :position_id,:user_id,:signal_id,:source_id,:trader_stream,:symbol,:side,:status,
                            :opened_at,:closed_at,:entry_price,:exit_price,:volume,:cash_pnl,:return_percent,
                            :net_pips,:pip_size,:model_500_pnl,:model_500_return_percent,
                            :planned_risk_percent,:close_reason,:broker_deal_count,:source_digest,:derived_at
                        )
                        ON CONFLICT (position_id) DO UPDATE SET
                            source_id=EXCLUDED.source_id,
                            trader_stream=EXCLUDED.trader_stream,
                            symbol=EXCLUDED.symbol,
                            side=EXCLUDED.side,
                            status=EXCLUDED.status,
                            opened_at=EXCLUDED.opened_at,
                            closed_at=EXCLUDED.closed_at,
                            entry_price=EXCLUDED.entry_price,
                            exit_price=EXCLUDED.exit_price,
                            volume=EXCLUDED.volume,
                            cash_pnl=EXCLUDED.cash_pnl,
                            return_percent=EXCLUDED.return_percent,
                            net_pips=EXCLUDED.net_pips,
                            pip_size=EXCLUDED.pip_size,
                            model_500_pnl=EXCLUDED.model_500_pnl,
                            model_500_return_percent=EXCLUDED.model_500_return_percent,
                            planned_risk_percent=EXCLUDED.planned_risk_percent,
                            close_reason=EXCLUDED.close_reason,
                            broker_deal_count=EXCLUDED.broker_deal_count,
                            source_digest=EXCLUDED.source_digest,
                            derived_at=EXCLUDED.derived_at
                        """
                    ),
                    outcome,
                )
                if outcome["cash_pnl"] is not None or outcome["exit_price"] is not None:
                    session.execute(
                        text(
                            """
                            UPDATE positions
                            SET pnl_amount=COALESCE(:cash_pnl,pnl_amount),
                                exit_price=COALESCE(:exit_price,exit_price),
                                closed_at=COALESCE(:closed_at,closed_at),
                                updated_at=CASE
                                    WHEN :cash_pnl IS NOT NULL OR :exit_price IS NOT NULL THEN :derived_at
                                    ELSE updated_at
                                END
                            WHERE id=:position_id
                            """
                        ),
                        outcome,
                    )
                if outcome["status"] in {"won", "lost", "breakeven"} and row["status"] == "open":
                    session.execute(
                        text(
                            """
                            UPDATE positions
                            SET status='closed',
                                closed_at=COALESCE(closed_at,:closed_at),
                                close_reason=COALESCE(close_reason,'external_close'),
                                updated_at=:derived_at
                            WHERE id=:position_id AND status='open'
                            """
                        ),
                        outcome,
                    )
                rebuilt += 1
            session.commit()
        return rebuilt

    def rebuild_summaries(self, user_id: UUID) -> int:
        with self._session_factory() as session:
            outcomes = session.execute(
                text(
                    """
                    SELECT o.*, COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label
                    FROM performance_trade_outcomes o
                    LEFT JOIN sources src ON src.id=o.source_id
                    WHERE o.user_id=:user_id
                    ORDER BY COALESCE(o.closed_at,o.opened_at), o.position_id
                    """
                ),
                {"user_id": user_id},
            ).mappings().all()
            session.execute(text("DELETE FROM performance_summaries WHERE user_id=:user_id"), {"user_id": user_id})
            if not outcomes:
                session.commit()
                return 0

            now = datetime.now(UTC)
            buckets: dict[tuple[str, datetime, datetime], list[Any]] = {}
            for row in outcomes:
                event_at = _utc(row["closed_at"] or row["opened_at"] or now)
                day = event_at.replace(hour=0, minute=0, second=0, microsecond=0)
                week = day - timedelta(days=day.weekday())
                month = day.replace(day=1)
                next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
                year = day.replace(month=1, day=1)
                periods = (
                    ("daily", day, day + timedelta(days=1)),
                    ("weekly", week, week + timedelta(days=7)),
                    ("monthly", month, next_month),
                    ("yearly", year, year.replace(year=year.year + 1)),
                    ("all_time", datetime(1970, 1, 1, tzinfo=UTC), datetime(9999, 1, 1, tzinfo=UTC)),
                )
                for period in periods:
                    buckets.setdefault(period, []).append(row)

            inserted = 0
            for (period_type, start, end), period_rows in sorted(buckets.items(), key=lambda item: item[0][1]):
                dimensions: dict[tuple[str, str, str], list[Any]] = {
                    ("portfolio", "all", "Portfolio"): period_rows
                }
                for row in period_rows:
                    if row["source_id"]:
                        source_key = f"source:{row['source_id']}"
                        dimensions.setdefault(("source", source_key, str(row["source_label"])), []).append(row)
                        if row["trader_stream"]:
                            trader_key = f"trader:{row['source_id']}:{row['trader_stream']}"
                            trader_label = f"{row['source_label']} · {row['trader_stream']}"
                            dimensions.setdefault(("trader", trader_key, trader_label), []).append(row)
                    symbol = str(row["symbol"] or "")
                    if symbol:
                        dimensions.setdefault(("symbol", f"symbol:{symbol}", symbol), []).append(row)

                for (dimension_type, dimension_key, dimension_label), dimension_rows in dimensions.items():
                    metrics = self._summary_metrics(user_id, dimension_rows, start, end)
                    session.execute(
                        text(
                            """
                            INSERT INTO performance_summaries (
                                user_id,period_type,period_start,period_end,dimension_type,dimension_key,
                                dimension_label,total_trades,wins,losses,breakeven,open_trades,cash_pnl,
                                return_percent,net_pips,gross_profit_pips,gross_loss_pips,
                                model_500_pnl,model_500_return_percent,source_digest,generated_at
                            ) VALUES (
                                :user_id,:period_type,:period_start,:period_end,:dimension_type,:dimension_key,
                                :dimension_label,:total_trades,:wins,:losses,:breakeven,:open_trades,:cash_pnl,
                                :return_percent,:net_pips,:gross_profit_pips,:gross_loss_pips,
                                :model_500_pnl,:model_500_return_percent,:source_digest,:generated_at
                            )
                            """
                        ),
                        {
                            "user_id": user_id,
                            "period_type": period_type,
                            "period_start": start,
                            "period_end": end,
                            "dimension_type": dimension_type,
                            "dimension_key": dimension_key,
                            "dimension_label": dimension_label,
                            **metrics,
                            "generated_at": now,
                        },
                    )
                    inserted += 1
            session.commit()
            return inserted

    def _account(self, user_id: UUID) -> Any | None:
        with self._session_factory() as session:
            return session.execute(
                text(
                    """
                    SELECT id,metaapi_account_id,metaapi_token_ciphertext,status
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status!='revoked'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()

    def _mapped_positions(self, user_id: UUID) -> list[Any]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        """
                        SELECT p.id,p.user_id,p.signal_id,p.tp_index,p.take_profit,p.planned_risk_percent,
                               p.volume,p.stop_loss,p.broker_position_id,p.broker_order_id,p.broker_client_id,
                               p.status,p.entry_price,p.exit_price,p.opened_at,p.closed_at,p.pnl_amount,
                               p.pnl_percent,p.close_reason,p.created_at,p.updated_at,
                               s.symbol,s.side,s.stop_loss AS signal_stop_loss,s.risk_multiplier,
                               s.source_id,s.original_text,
                               COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_alias
                        FROM positions p
                        JOIN signals s ON s.id=p.signal_id
                        LEFT JOIN sources src ON src.id=s.source_id
                        WHERE p.user_id=:user_id
                        ORDER BY p.created_at,p.id
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().all()
            )

    def _store_snapshot(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        payload: dict[str, object],
        captured_at: datetime,
    ) -> None:
        balance = payload.get("balance")
        credit = payload.get("credit") or 0
        equity = payload.get("equity")
        currency = str(payload.get("currency") or "USD")
        if balance is None or equity is None:
            return
        effective_balance = _d(balance) + _d(credit)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO performance_account_snapshots (
                        user_id,mt5_account_id,currency,balance,equity,captured_at
                    ) VALUES (:user_id,:mt5_account_id,:currency,:balance,:equity,:captured_at)
                    ON CONFLICT (mt5_account_id,captured_at) DO NOTHING
                    """
                ),
                {
                    "user_id": user_id,
                    "mt5_account_id": mt5_account_id,
                    "currency": currency,
                    "balance": effective_balance,
                    "equity": _d(equity),
                    "captured_at": captured_at,
                },
            )
            session.commit()

    def _store_deals(
        self,
        *,
        user_id: UUID,
        mt5_account_id: UUID,
        position_row: Any,
        payloads: list[dict[str, object]],
    ) -> int:
        added = 0
        trader = trader_stream_for(str(position_row["source_alias"] or ""), str(position_row["original_text"] or ""))
        with self._session_factory() as session:
            for payload in payloads:
                broker_deal_id = str(payload.get("id") or "").strip()
                deal_type = str(payload.get("type") or "").strip()
                if not broker_deal_id or not deal_type:
                    continue
                occurred_at = _parse_time(payload.get("time"))
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
                        RETURNING id
                        """
                    ),
                    {
                        "user_id": user_id,
                        "mt5_account_id": mt5_account_id,
                        "position_id": position_row["id"],
                        "signal_id": position_row["signal_id"],
                        "source_id": position_row["source_id"],
                        "trader_stream": trader,
                        "broker_deal_id": broker_deal_id,
                        "broker_position_id": str(payload.get("positionId") or position_row["broker_position_id"] or "") or None,
                        "broker_order_id": str(payload.get("orderId") or "") or None,
                        "broker_client_id": str(payload.get("clientId") or "") or None,
                        "deal_type": deal_type,
                        "entry_type": str(payload.get("entryType") or "") or None,
                        "symbol": str(payload.get("symbol") or position_row["symbol"] or "") or None,
                        "volume": (_d(payload.get("volume")) if payload.get("volume") is not None else None),
                        "price": (_d(payload.get("price")) if payload.get("price") is not None else None),
                        "profit": _d(payload.get("profit")),
                        "commission": _d(payload.get("commission")),
                        "swap": _d(payload.get("swap")),
                        "occurred_at": occurred_at,
                        "broker_time": str(payload.get("brokerTime") or "") or None,
                        "raw_payload": json.dumps(payload, separators=(",", ":"), default=str),
                    },
                ).first()
                if result is not None:
                    added += 1
            session.commit()
        return added

    def _derive_outcome(self, session: Session, row: Any, deals: list[Any]) -> dict[str, Any]:
        entry_deals = [d for d in deals if str(d["entry_type"] or "").upper() == "DEAL_ENTRY_IN"]
        exit_deals = [
            d for d in deals
            if str(d["entry_type"] or "").upper() in {"DEAL_ENTRY_OUT", "DEAL_ENTRY_OUT_BY"}
        ]
        entry_price = self._weighted_price(entry_deals) or (Decimal(str(row["entry_price"])) if row["entry_price"] is not None else None)
        exit_price = self._weighted_price(exit_deals) or (Decimal(str(row["exit_price"])) if row["exit_price"] is not None else None)
        entry_volume = sum((_d(d["volume"]) for d in entry_deals), Decimal("0"))
        exit_volume = sum((_d(d["volume"]) for d in exit_deals), Decimal("0"))
        broker_complete = bool(exit_deals) and (entry_volume == 0 or exit_volume + Decimal("0.00000001") >= entry_volume)
        cash = None
        if broker_complete:
            cash = _money(sum((_d(d["profit"]) + _d(d["commission"]) + _d(d["swap"]) for d in deals), Decimal("0")))

        status = "open" if str(row["status"]) == "open" else "closed_unknown"
        if str(row["status"]) == "planned" and row["broker_order_id"] and not row["broker_position_id"]:
            status = "pending"
        if broker_complete and cash is not None:
            status = "won" if cash > 0 else "lost" if cash < 0 else "breakeven"

        symbol = str(row["symbol"] or "")
        side = str(row["side"] or "").upper()
        pip_size = XAUUSD_PIP_SIZE if symbol.upper() == "XAUUSD" else None
        net_pips = None
        if broker_complete and entry_price is not None and exit_price is not None and pip_size:
            move = (exit_price - entry_price) if side == "BUY" else (entry_price - exit_price)
            net_pips = _money(move / pip_size)

        model_pnl = None
        model_return = None
        stop = (
            Decimal(str(row["signal_stop_loss"]))
            if row["signal_stop_loss"] is not None
            else Decimal(str(row["stop_loss"]))
            if row["stop_loss"] is not None
            else None
        )
        if broker_complete and entry_price is not None and exit_price is not None and stop is not None:
            risk_distance = abs(entry_price - stop)
            if risk_distance > 0:
                move = (exit_price - entry_price) if side == "BUY" else (entry_price - exit_price)
                risk_multiplier = _d(row["risk_multiplier"], Decimal("1"))
                model_risk_cash = MODEL_BALANCE * (MODEL_NORMAL_RISK_PERCENT / Decimal("100")) * risk_multiplier
                model_pnl = _money(model_risk_cash * (move / risk_distance))
                model_return = _pct(model_pnl / MODEL_BALANCE * Decimal("100"))

        return_percent = None
        if cash is not None and row["opened_at"] is not None:
            baseline = session.execute(
                text(
                    """
                    SELECT balance
                    FROM performance_account_snapshots
                    WHERE user_id=:user_id AND captured_at<=:opened_at
                    ORDER BY captured_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": row["user_id"], "opened_at": row["opened_at"]},
            ).scalar_one_or_none()
            if baseline is not None and _d(baseline) > 0:
                return_percent = _pct(cash / _d(baseline) * Decimal("100"))

        digest_payload = [
            {
                "id": str(d["broker_deal_id"]),
                "entry": str(d["entry_type"] or ""),
                "type": str(d["deal_type"]),
                "price": str(d["price"]),
                "volume": str(d["volume"]),
                "profit": str(d["profit"]),
                "commission": str(d["commission"]),
                "swap": str(d["swap"]),
                "time": _utc(d["occurred_at"]).isoformat(),
            }
            for d in deals
        ]
        digest = sha256(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        trader = trader_stream_for(str(row["source_alias"] or ""), str(row["original_text"] or ""))
        closed_at = (
            max((_utc(d["occurred_at"]) for d in exit_deals), default=None)
            if broker_complete
            else row["closed_at"]
        )

        return {
            "position_id": row["id"],
            "user_id": row["user_id"],
            "signal_id": row["signal_id"],
            "source_id": row["source_id"],
            "trader_stream": trader,
            "symbol": symbol,
            "side": side,
            "status": status,
            "opened_at": row["opened_at"],
            "closed_at": closed_at,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "volume": (entry_volume if entry_volume > 0 else row["volume"]),
            "cash_pnl": cash,
            "return_percent": return_percent,
            "net_pips": net_pips,
            "pip_size": pip_size,
            "model_500_pnl": model_pnl,
            "model_500_return_percent": model_return,
            "planned_risk_percent": row["planned_risk_percent"],
            "close_reason": row["close_reason"],
            "broker_deal_count": len(deals),
            "source_digest": digest,
            "derived_at": datetime.now(UTC),
        }

    @staticmethod
    def _weighted_price(deals: list[Any]) -> Decimal | None:
        values = [
            (_d(item["price"]), _d(item["volume"]))
            for item in deals
            if item["price"] is not None and item["volume"] is not None and _d(item["volume"]) > 0
        ]
        if not values:
            return None
        total_volume = sum((volume for _, volume in values), Decimal("0"))
        if total_volume <= 0:
            return None
        return sum((price * volume for price, volume in values), Decimal("0")) / total_volume

    def _summary_metrics(
        self,
        user_id: UUID,
        rows: list[Any],
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        closed = [r for r in rows if r["status"] in {"won", "lost", "breakeven", "closed_unknown"}]
        known = [r for r in closed if r["cash_pnl"] is not None]
        open_rows = [r for r in rows if r["status"] in {"open", "pending"}]
        wins = sum(1 for r in known if _d(r["cash_pnl"]) > 0)
        losses = sum(1 for r in known if _d(r["cash_pnl"]) < 0)
        breakeven = sum(1 for r in known if _d(r["cash_pnl"]) == 0)
        cash = _money(sum((_d(r["cash_pnl"]) for r in known), Decimal("0")))
        model = _money(sum((_d(r["model_500_pnl"]) for r in known if r["model_500_pnl"] is not None), Decimal("0")))
        model_return = _pct(model / MODEL_BALANCE * Decimal("100"))

        symbols = {str(r["symbol"]) for r in known if r["net_pips"] is not None}
        pips_known = bool(known) and all(r["net_pips"] is not None for r in known)
        net_pips = gross_profit_pips = gross_loss_pips = None
        if pips_known and len(symbols) <= 1:
            pips = [_d(r["net_pips"]) for r in known]
            net_pips = _money(sum(pips, Decimal("0")))
            gross_profit_pips = _money(sum((p for p in pips if p > 0), Decimal("0")))
            gross_loss_pips = _money(sum((p for p in pips if p < 0), Decimal("0")))

        return_percent = self._period_return_percent(user_id, start, cash)
        digest = sha256(
            "|".join(sorted(str(r["source_digest"]) for r in rows)).encode("utf-8")
        ).hexdigest()
        return {
            "total_trades": len(closed),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "open_trades": len(open_rows),
            "cash_pnl": cash,
            "return_percent": return_percent,
            "net_pips": net_pips,
            "gross_profit_pips": gross_profit_pips,
            "gross_loss_pips": gross_loss_pips,
            "model_500_pnl": model,
            "model_500_return_percent": model_return,
            "source_digest": digest,
        }

    def _period_return_percent(self, user_id: UUID, period_start: datetime, cash_pnl: Decimal) -> Decimal | None:
        if period_start.year <= 1970:
            return None
        with self._session_factory() as session:
            baseline = session.execute(
                text(
                    """
                    SELECT balance
                    FROM performance_account_snapshots
                    WHERE user_id=:user_id AND captured_at<=:period_start
                    ORDER BY captured_at DESC
                    LIMIT 1
                    """
                ),
                {"user_id": user_id, "period_start": period_start},
            ).scalar_one_or_none()
        if baseline is None or _d(baseline) <= 0:
            return None
        return _pct(cash_pnl / _d(baseline) * Decimal("100"))

    def _window(
        self,
        user_id: UUID,
        key: str,
        label: str,
        since: datetime | None,
        now: datetime,
    ) -> Day33PerformanceWindow:
        with self._session_factory() as session:
            clauses = ["user_id=:user_id"]
            params: dict[str, Any] = {"user_id": user_id}
            if since is not None:
                clauses.append("(closed_at>=:since OR (status IN ('open','pending') AND opened_at>=:since))")
                params["since"] = since
            rows = session.execute(
                text(
                    f"""
                    SELECT status,symbol,cash_pnl,return_percent,net_pips,model_500_pnl
                    FROM performance_trade_outcomes
                    WHERE {' AND '.join(clauses)}
                    """
                ),
                params,
            ).mappings().all()

        known = [r for r in rows if r["status"] in {"won", "lost", "breakeven"} and r["cash_pnl"] is not None]
        cash = _money(sum((_d(r["cash_pnl"]) for r in known), Decimal("0")))
        model = _money(sum((_d(r["model_500_pnl"]) for r in known if r["model_500_pnl"] is not None), Decimal("0")))
        wins = sum(1 for r in known if r["status"] == "won")
        losses = sum(1 for r in known if r["status"] == "lost")
        breakeven = sum(1 for r in known if r["status"] == "breakeven")
        decided = wins + losses
        win_rate = _pct(Decimal(wins) / Decimal(decided) * Decimal("100")) if decided else None
        symbols = {str(r["symbol"]) for r in known if r["net_pips"] is not None}
        mixed = len(symbols) > 1
        net_pips = None
        if known and not mixed and all(r["net_pips"] is not None for r in known):
            net_pips = _money(sum((_d(r["net_pips"]) for r in known), Decimal("0")))
        real_return = None
        if since is not None:
            real_return = self._period_return_percent(user_id, since, cash)

        return Day33PerformanceWindow(
            key=key,
            label=label,
            cash_pnl=cash,
            return_percent=real_return,
            model_500_pnl=model,
            model_500_return_percent=_pct(model / MODEL_BALANCE * Decimal("100")),
            closed_trades=len(known),
            wins=wins,
            losses=losses,
            breakeven=breakeven,
            open_trades=sum(1 for r in rows if r["status"] in {"open", "pending"}),
            win_rate_percent=win_rate,
            net_pips=net_pips,
            mixed_instrument_pips=mixed,
        )

    def _timeline_rows(self, user_id: UUID) -> list[Any]:
        with self._session_factory() as session:
            return list(
                session.execute(
                    text(
                        """
                        SELECT s.id AS signal_id,s.symbol,s.side,s.source_id,
                               COALESCE(src.source_alias,src.chat_title,'Unknown source') AS source_label,
                               MIN(o.opened_at) AS opened_at,MAX(o.closed_at) AS closed_at,
                               COUNT(o.position_id)::int AS position_count,
                               COUNT(*) FILTER (WHERE o.status='open')::int AS open_positions,
                               COUNT(*) FILTER (WHERE o.status='pending')::int AS pending_positions,
                               COUNT(*) FILTER (WHERE o.status IN ('won','lost','breakeven','closed_unknown'))::int AS closed_positions,
                               SUM(o.cash_pnl) FILTER (WHERE o.cash_pnl IS NOT NULL) AS cash_pnl,
                               CASE
                                   WHEN COUNT(DISTINCT o.symbol) FILTER (WHERE o.net_pips IS NOT NULL)<=1
                                    AND COUNT(*) FILTER (WHERE o.status IN ('won','lost','breakeven') AND o.net_pips IS NULL)=0
                                   THEN SUM(o.net_pips)
                                   ELSE NULL
                               END AS net_pips,
                               SUM(o.model_500_pnl) FILTER (WHERE o.model_500_pnl IS NOT NULL) AS model_500_pnl,
                               MAX(o.trader_stream) FILTER (WHERE o.trader_stream IS NOT NULL) AS trader_stream,
                               MAX(o.close_reason) FILTER (WHERE o.close_reason IS NOT NULL) AS close_reason,
                               BOOL_OR(o.status='won') AS has_win,
                               BOOL_OR(o.status='lost') AS has_loss,
                               BOOL_OR(o.status='breakeven') AS has_breakeven,
                               BOOL_OR(o.status='closed_unknown') AS has_unknown
                        FROM performance_trade_outcomes o
                        JOIN signals s ON s.id=o.signal_id
                        LEFT JOIN sources src ON src.id=s.source_id
                        WHERE o.user_id=:user_id
                        GROUP BY s.id,s.symbol,s.side,s.source_id,src.source_alias,src.chat_title
                        ORDER BY COALESCE(MAX(o.opened_at),MAX(o.closed_at),MAX(o.derived_at)) DESC
                        """
                    ),
                    {"user_id": user_id},
                ).mappings().all()
            )

    @staticmethod
    def _timeline_trade(row: Any, *, provider_visible: bool) -> Day33TimelineTrade:
        open_count = int(row["open_positions"])
        pending_count = int(row["pending_positions"])
        closed_count = int(row["closed_positions"])
        cash = Decimal(str(row["cash_pnl"])) if row["cash_pnl"] is not None else None
        if open_count:
            status, label, color = "open", "Open", "blue"
        elif pending_count:
            status, label, color = "pending", "Pending", "amber"
        elif closed_count:
            if cash is None or bool(row["has_unknown"]):
                status, label, color = "closed_unknown", "Closed", "grey"
            elif cash > 0:
                status, label, color = "won", "Closed · Win", "green"
            elif cash < 0:
                status, label, color = "lost", "Closed · Loss", "red"
            else:
                status, label, color = "breakeven", "Closed · BE", "grey"
        else:
            status, label, color = "closed_unknown", "Closed", "grey"

        source_id = row["source_id"]
        trader = str(row["trader_stream"]) if row["trader_stream"] else None
        return Day33TimelineTrade(
            signal_id=row["signal_id"],
            symbol=str(row["symbol"] or ""),
            side=str(row["side"] or ""),
            status=status,
            status_label=label,
            status_color=color,
            source_label=(str(row["source_label"]) if provider_visible else None),
            trader_stream=(trader if provider_visible else None),
            source_color_index=(stable_color_index(source_id, trader) if provider_visible and source_id else None),
            opened_at=row["opened_at"],
            closed_at=row["closed_at"],
            position_count=int(row["position_count"]),
            open_positions=open_count,
            pending_positions=pending_count,
            closed_positions=closed_count,
            cash_pnl=cash,
            net_pips=(Decimal(str(row["net_pips"])) if row["net_pips"] is not None else None),
            model_500_pnl=(Decimal(str(row["model_500_pnl"])) if row["model_500_pnl"] is not None else None),
            close_reason=(str(row["close_reason"]) if row["close_reason"] else None),
        )
