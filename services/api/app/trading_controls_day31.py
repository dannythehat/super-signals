"""Day 31 per-user risk settings and trading activation/stop controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_gateway import MetaApiGatewayError
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher

_ALLOWED_RISKS = {Decimal("0.5"), Decimal("1.0"), Decimal("1.5"), Decimal("2.0")}


class Day31TradingControlError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Day31TradingControlView:
    user_id: UUID
    risk_percent: Decimal
    allow_double_lot: bool
    effective_normal_risk_percent: Decimal
    effective_double_lot_risk_percent: Decimal
    trading_status: str
    activated_at: datetime | None
    stopped_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Day31ActivationPreview:
    settings: Day31TradingControlView
    ready: bool
    requirements: tuple[dict[str, object], ...]
    confirmation_title: str
    confirmation_message: str


@dataclass(frozen=True, slots=True)
class Day31StopResult:
    settings: Day31TradingControlView
    broker_actions_sent: int
    positions_closed: int
    external_positions_reconciled: int
    already_stopped: bool


@dataclass(frozen=True, slots=True)
class _Account:
    local_id: UUID
    account_id: str
    login: str
    server: str
    token_ciphertext: bytes


class Day31TradingControlService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        cipher: MetaApiTokenCipher,
        read_gateway: MetaApiReadGateway,
        trade_gateway: MetaApiTradeGateway,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._read = read_gateway
        self._trade = trade_gateway

    def get_settings(self, user_id: UUID) -> Day31TradingControlView:
        self._ensure_user_control(user_id)
        return self._load_control(user_id)

    def update_risk(
        self,
        *,
        user_id: UUID,
        risk_percent: Decimal,
        allow_double_lot: bool,
    ) -> Day31TradingControlView:
        risk = Decimal(str(risk_percent))
        if risk not in _ALLOWED_RISKS:
            raise Day31TradingControlError("risk_percent_invalid")
        self._ensure_user_control(user_id)
        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT risk_percent, allow_double_lot, trading_status
                    FROM user_trading_controls
                    WHERE user_id = :user_id
                    FOR UPDATE
                    """
                ),
                {"user_id": user_id},
            ).mappings().one()
            changed = (
                Decimal(str(row["risk_percent"])) != risk
                or bool(row["allow_double_lot"]) != bool(allow_double_lot)
            )
            if changed:
                # Changing risk while active stops new trades. The user must see
                # and confirm the new effective risk before automation resumes.
                session.execute(
                    text(
                        """
                        UPDATE user_trading_controls
                        SET risk_percent = :risk_percent,
                            allow_double_lot = :allow_double_lot,
                            trading_status = CASE WHEN trading_status = 'active' THEN 'stopped' ELSE trading_status END,
                            stopped_at = CASE WHEN trading_status = 'active' THEN :now ELSE stopped_at END,
                            updated_at = :now
                        WHERE user_id = :user_id
                        """
                    ),
                    {
                        "user_id": user_id,
                        "risk_percent": risk,
                        "allow_double_lot": bool(allow_double_lot),
                        "now": now,
                    },
                )
                session.add(
                    AuditEvent(
                        actor_user_id=user_id,
                        event_type="trading.risk_settings_changed",
                        entity_type="user_trading_controls",
                        entity_id=user_id,
                        payload={
                            "risk_percent": str(risk),
                            "allow_double_lot": bool(allow_double_lot),
                            "automation_stopped_for_reconfirmation": str(row["trading_status"]) == "active",
                        },
                    )
                )
                session.commit()
        return self._load_control(user_id)

    def activation_preview(self, user_id: UUID) -> Day31ActivationPreview:
        settings = self.get_settings(user_id)
        requirements = self._activation_requirements(user_id)
        ready = all(bool(item["passed"]) for item in requirements)
        double_text = (
            f"{self._fmt(settings.effective_double_lot_risk_percent)}% risk per position"
            if settings.allow_double_lot
            else "double-lot instructions are disabled; base risk remains unchanged"
        )
        return Day31ActivationPreview(
            settings=settings,
            ready=ready,
            requirements=requirements,
            confirmation_title="Activate automated trading?",
            confirmation_message=(
                "Trades will be activated immediately when an approved signal passes all execution gates. "
                f"Normal signals use {self._fmt(settings.effective_normal_risk_percent)}% risk per position. "
                f"Provider DOUBLE LOTSIZE signals use {double_text}."
            ),
        )

    def activate(self, *, user_id: UUID, confirmed: bool) -> Day31TradingControlView:
        if not confirmed:
            raise Day31TradingControlError("activation_confirmation_required")
        preview = self.activation_preview(user_id)
        if not preview.ready:
            raise Day31TradingControlError("activation_requirements_incomplete")
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE user_trading_controls
                    SET trading_status = 'active', activated_at = :now, updated_at = :now
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id, "now": now},
            )
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="trading.automation_activated",
                    entity_type="user_trading_controls",
                    entity_id=user_id,
                    payload={
                        "risk_percent": str(preview.settings.risk_percent),
                        "allow_double_lot": preview.settings.allow_double_lot,
                        "effective_normal_risk_percent": str(preview.settings.effective_normal_risk_percent),
                        "effective_double_lot_risk_percent": str(preview.settings.effective_double_lot_risk_percent),
                        "requirements_passed": True,
                    },
                )
            )
            session.commit()
        return self._load_control(user_id)

    def is_active(self, user_id: UUID) -> bool:
        try:
            return self._load_control(user_id).trading_status == "active"
        except Day31TradingControlError:
            return False

    async def stop_and_close(self, user_id: UUID) -> Day31StopResult:
        settings = self.get_settings(user_id)
        already_stopped = settings.trading_status == "stopped"
        now = datetime.now(UTC)

        # Stop new trades before touching the broker. Any later broker failure
        # leaves automation stopped rather than silently reactivating it.
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE user_trading_controls
                    SET trading_status = 'stopped', stopped_at = :now, updated_at = :now
                    WHERE user_id = :user_id
                    """
                ),
                {"user_id": user_id, "now": now},
            )
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="trading.stop_requested",
                    entity_type="user_trading_controls",
                    entity_id=user_id,
                    payload={"close_bot_positions": True, "automation_blocked_first": True},
                )
            )
            session.commit()

        local_positions = self._load_open_mapped_positions(user_id)
        if not local_positions:
            return Day31StopResult(
                settings=self._load_control(user_id),
                broker_actions_sent=0,
                positions_closed=0,
                external_positions_reconciled=0,
                already_stopped=already_stopped,
            )

        account = self._connected_live_account(user_id)
        if account is None:
            self._audit_stop_failure(user_id, "mt5_account_not_connected")
            raise Day31TradingControlError("mt5_account_not_connected", retryable=True)
        try:
            token = self._cipher.decrypt(account.token_ciphertext)
        except BrokerCredentialDecryptionError as exc:
            self._audit_stop_failure(user_id, "broker_credential_decryption_failed")
            raise Day31TradingControlError("broker_credential_decryption_failed") from exc

        broker_actions = 0
        positions_closed = 0
        reconciled = 0
        try:
            region = await self._read.resolve_account_region(
                token=token,
                account_id=account.account_id,
            )
            broker_payload = await self._read.read_positions(
                token=token,
                account_id=account.account_id,
                region=region,
            )
            broker_ids = {
                str(item.get("id") or "").strip()
                for item in broker_payload
                if str(item.get("id") or "").strip()
            }
            for position_id, broker_position_id in local_positions:
                if broker_position_id not in broker_ids:
                    self._mark_closed(position_id, "external_close")
                    reconciled += 1
                    continue
                await self._trade.close_position(
                    token=token,
                    account_id=account.account_id,
                    region=region,
                    position_id=broker_position_id,
                )
                broker_actions += 1
                positions_closed += 1
                self._mark_closed(position_id, "user_stop_close")
        except MetaApiGatewayError as exc:
            self._audit_stop_failure(
                user_id,
                exc.code,
                broker_actions_sent=broker_actions,
                positions_closed=positions_closed,
                external_positions_reconciled=reconciled,
            )
            raise Day31TradingControlError(exc.code, retryable=exc.retryable) from exc

        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="trading.stop_close_completed",
                    entity_type="user_trading_controls",
                    entity_id=user_id,
                    payload={
                        "broker_actions_sent": broker_actions,
                        "positions_closed": positions_closed,
                        "external_positions_reconciled": reconciled,
                        "manual_or_unmapped_positions_touched": False,
                        "automation_status": "stopped",
                    },
                )
            )
            session.commit()
        return Day31StopResult(
            settings=self._load_control(user_id),
            broker_actions_sent=broker_actions,
            positions_closed=positions_closed,
            external_positions_reconciled=reconciled,
            already_stopped=already_stopped,
        )

    def _activation_requirements(self, user_id: UUID) -> tuple[dict[str, object], ...]:
        with self._session_factory() as session:
            user_active = bool(
                session.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM users WHERE id=:id AND status='active')"),
                    {"id": user_id},
                )
            )
            approval = session.execute(
                text(
                    """
                    SELECT login, server
                    FROM mt5_account_approvals
                    WHERE user_id=:user_id AND status='active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
            account = session.execute(
                text(
                    """
                    SELECT login, server, status, account_environment
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id AND status!='revoked'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        approved = approval is not None
        connected = bool(
            approval
            and account
            and str(account["status"]) == "connected"
            and str(account["account_environment"]) == "live"
            and str(account["login"]) == str(approval["login"])
            and str(account["server"]).casefold() == str(approval["server"]).casefold()
        )
        return (
            {"key": "active_account", "label": "Super Signals account active", "passed": user_active},
            {"key": "mt5_approved", "label": "Vantage MT5 account approved", "passed": approved},
            {"key": "mt5_connected", "label": "Approved live MT5 account connected", "passed": connected},
            {"key": "risk_selected", "label": "Risk settings selected", "passed": True},
        )

    def _ensure_user_control(self, user_id: UUID) -> None:
        with self._session_factory() as session:
            eligible = bool(
                session.scalar(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM users u
                            JOIN user_roles ur ON ur.user_id=u.id
                            JOIN roles r ON r.id=ur.role_id
                            WHERE u.id=:user_id AND u.status='active' AND r.name='user'
                        )
                        """
                    ),
                    {"user_id": user_id},
                )
            )
            if not eligible:
                raise Day31TradingControlError("trading_user_not_eligible")
            session.execute(
                text(
                    """
                    INSERT INTO user_trading_controls (user_id)
                    VALUES (:user_id)
                    ON CONFLICT (user_id) DO NOTHING
                    """
                ),
                {"user_id": user_id},
            )
            session.commit()

    def _load_control(self, user_id: UUID) -> Day31TradingControlView:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT user_id, risk_percent, allow_double_lot, trading_status,
                           activated_at, stopped_at, updated_at
                    FROM user_trading_controls WHERE user_id=:user_id
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            raise Day31TradingControlError("trading_controls_not_configured")
        risk = Decimal(str(row["risk_percent"]))
        allow_double = bool(row["allow_double_lot"])
        return Day31TradingControlView(
            user_id=row["user_id"],
            risk_percent=risk,
            allow_double_lot=allow_double,
            effective_normal_risk_percent=risk,
            effective_double_lot_risk_percent=risk * (Decimal("2") if allow_double else Decimal("1")),
            trading_status=str(row["trading_status"]),
            activated_at=row["activated_at"],
            stopped_at=row["stopped_at"],
            updated_at=row["updated_at"],
        )

    def _connected_live_account(self, user_id: UUID) -> _Account | None:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT id, metaapi_account_id, login, server, metaapi_token_ciphertext
                    FROM mt5_accounts
                    WHERE owner_user_id=:user_id
                      AND account_environment='live'
                      AND status='connected'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            return None
        return _Account(
            local_id=row["id"],
            account_id=str(row["metaapi_account_id"]),
            login=str(row["login"]),
            server=str(row["server"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )

    def _load_open_mapped_positions(self, user_id: UUID) -> tuple[tuple[UUID, str], ...]:
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, broker_position_id
                    FROM positions
                    WHERE user_id=:user_id
                      AND status='open'
                      AND broker_position_id IS NOT NULL
                    ORDER BY created_at, id
                    """
                ),
                {"user_id": user_id},
            ).all()
        return tuple((row[0], str(row[1])) for row in rows)

    def _mark_closed(self, position_id: UUID, reason: str) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE positions
                    SET status='closed', closed_at=COALESCE(closed_at,:now),
                        close_reason=COALESCE(close_reason,:reason), updated_at=:now
                    WHERE id=:id AND status='open'
                    """
                ),
                {"id": position_id, "now": now, "reason": reason},
            )
            session.commit()

    def _audit_stop_failure(
        self,
        user_id: UUID,
        error_code: str,
        *,
        broker_actions_sent: int = 0,
        positions_closed: int = 0,
        external_positions_reconciled: int = 0,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                AuditEvent(
                    actor_user_id=user_id,
                    event_type="trading.stop_close_failed",
                    entity_type="user_trading_controls",
                    entity_id=user_id,
                    payload={
                        "error_code": error_code,
                        "broker_actions_sent": broker_actions_sent,
                        "positions_closed": positions_closed,
                        "external_positions_reconciled": external_positions_reconciled,
                        "automation_status": "stopped",
                        "automatic_reactivation": False,
                    },
                )
            )
            session.commit()

    @staticmethod
    def _fmt(value: Decimal) -> str:
        return format(value.normalize(), "f")
