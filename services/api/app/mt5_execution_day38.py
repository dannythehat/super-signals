"""Day 38 LIVE-account adapter over the single Super Signals trading engine.

Trading-policy parity is a product invariant: DEMO and LIVE accounts must interpret and
execute the same canonical provider signal identically. This class therefore inherits
the exact PaperFreshStartExecutionService used by the Owner paper account. The only
LIVE-specific behaviour here is account/user eligibility and credential selection.

When LIVE execution is enabled by the outer distribution switch, pending/layered trades,
per-provider-section risk, fresh market execution, atomic compensation and all later
execution-path reliability fixes therefore come from the same implementation as paper.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.mt5_crypto import BrokerCredentialDecryptionError
from app.mt5_execution_day26 import (
    Day26ExecutionError,
    Day26ExecutionResult,
    _AccountInput,
    _SignalInput,
)
from app.paper_fresh_start_execution import PaperFreshStartExecutionService


class Day38LiveUserExecutionService(PaperFreshStartExecutionService):
    """Run the exact paper-tested trading engine against one approved LIVE account."""

    async def execute_live_user_signal(
        self,
        *,
        user_id: UUID,
        signal_id: UUID,
    ) -> Day26ExecutionResult:
        risk_percent, allow_double_lot = self._load_live_preferences(user_id)
        return await self.execute_owner_demo_signal(
            owner_user_id=user_id,
            signal_id=signal_id,
            risk_percent=risk_percent,
            double_lot_approved=allow_double_lot,
        )

    def _load_live_preferences(self, user_id: UUID) -> tuple[str, bool]:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT utc.risk_percent, utc.allow_double_lot
                    FROM users AS u
                    JOIN user_roles AS ur ON ur.user_id=u.id
                    JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                    JOIN user_trading_controls AS utc ON utc.user_id=u.id
                    WHERE u.id=:user_id
                      AND u.status='active'
                      AND utc.trading_status='active'
                    LIMIT 1
                    """
                ),
                {"user_id": user_id},
            ).mappings().first()
        if row is None:
            raise Day26ExecutionError("day38_user_not_execution_active")
        return str(row["risk_percent"]), bool(row["allow_double_lot"])

    def _load_inputs(self, user_id: UUID, signal_id: UUID) -> tuple[_SignalInput, _AccountInput]:
        """Load an ordinary canonical market signal plus the approved LIVE account."""
        with self._session_factory() as session:
            signal_row = session.execute(
                text(
                    """
                    SELECT id, symbol, side, order_type, entry_low, entry_high,
                           stop_loss, take_profits, has_open_runner, parser_status,
                           risk_multiplier, source_revision_index, source_posted_at
                    FROM signals
                    WHERE id=:signal_id
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if signal_row is None:
                raise Day26ExecutionError("signal_not_found")
            if str(signal_row["parser_status"] or "") != "accepted":
                raise Day26ExecutionError("signal_not_accepted")
            if str(signal_row["order_type"] or "").lower() != "market":
                raise Day26ExecutionError("day26_market_signal_required")

            existing_positions = int(
                session.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM positions
                        WHERE signal_id=:signal_id AND user_id=:user_id
                        """
                    ),
                    {"signal_id": signal_id, "user_id": user_id},
                ).scalar_one()
            )
            if existing_positions > 0:
                raise Day26ExecutionError("signal_execution_already_started")

            cancelled = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1
                            FROM signal_lifecycle_events
                            WHERE signal_id=:signal_id
                              AND event_type='cancel'
                        )
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalar_one()
            )
            if cancelled:
                raise Day26ExecutionError("signal_cancelled")

            eligibility = self._live_account_row(session, user_id)
            if eligibility is None:
                raise Day26ExecutionError("day38_user_not_execution_ready")
            self._validate_live_account_row(eligibility)

        symbol = str(signal_row["symbol"] or "").strip().upper()
        side = str(signal_row["side"] or "").strip().upper()
        if symbol != "XAUUSD":
            raise Day26ExecutionError("day26_xauusd_required")
        if side not in {"BUY", "SELL"}:
            raise Day26ExecutionError("trade_side_invalid")

        entry_low = self._required_decimal(signal_row["entry_low"], "signal_entry_invalid")
        entry_high = self._required_decimal(signal_row["entry_high"], "signal_entry_invalid")
        if entry_high < entry_low:
            raise Day26ExecutionError("signal_entry_invalid")
        stop_loss = self._required_decimal(signal_row["stop_loss"], "signal_stop_loss_invalid")
        take_profits = self._take_profits(signal_row["take_profits"])
        has_open_runner = bool(signal_row["has_open_runner"])
        if not self._directionally_valid(
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
        ):
            raise Day26ExecutionError("strict_directional_validation_failed")

        risk_multiplier = self._required_decimal(
            signal_row["risk_multiplier"], "signal_risk_multiplier_invalid"
        )
        source_posted_at = signal_row["source_posted_at"]
        if source_posted_at is None:
            raise Day26ExecutionError("signal_posted_at_invalid")

        signal = _SignalInput(
            signal_id=signal_id,
            symbol=symbol,
            side=side,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss,
            take_profits=take_profits,
            has_open_runner=has_open_runner,
            signal_requests_double_lot=risk_multiplier > Decimal("1"),
            source_revision_index=int(signal_row["source_revision_index"]),
            source_posted_at=source_posted_at,
        )
        account = _AccountInput(
            local_account_id=UUID(str(eligibility["mt5_account_id"])),
            metaapi_account_id=str(eligibility["metaapi_account_id"]),
            token_ciphertext=bytes(eligibility["metaapi_token_ciphertext"]),
        )
        return signal, account

    def _load_demo_account(self, user_id: UUID, signal_id: UUID) -> _AccountInput:
        """Critical-engine account adapter: use the same engine with a LIVE account."""
        with self._session_factory() as session:
            existing = int(
                session.execute(
                    text(
                        "SELECT COUNT(*) FROM positions WHERE signal_id=:signal_id AND user_id=:user_id"
                    ),
                    {"signal_id": signal_id, "user_id": user_id},
                ).scalar_one()
            )
            if existing:
                raise Day26ExecutionError("signal_execution_already_started")
            cancelled = bool(
                session.execute(
                    text(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM signal_lifecycle_events
                            WHERE signal_id=:signal_id AND event_type='cancel'
                        )
                        """
                    ),
                    {"signal_id": signal_id},
                ).scalar_one()
            )
            if cancelled:
                raise Day26ExecutionError("signal_cancelled")
            row = self._live_account_row(session, user_id)
        if row is None:
            raise Day26ExecutionError("day38_user_not_execution_ready")
        self._validate_live_account_row(row)
        return _AccountInput(
            local_account_id=UUID(str(row["mt5_account_id"])),
            metaapi_account_id=str(row["metaapi_account_id"]),
            token_ciphertext=bytes(row["metaapi_token_ciphertext"]),
        )

    @staticmethod
    def _live_account_row(session, user_id: UUID):  # noqa: ANN001
        return session.execute(
            text(
                """
                SELECT
                    m.id AS mt5_account_id,
                    m.metaapi_account_id,
                    m.metaapi_token_ciphertext,
                    m.account_environment,
                    m.status,
                    m.login,
                    m.server,
                    a.login AS approved_login,
                    a.server AS approved_server
                FROM users AS u
                JOIN user_roles AS ur ON ur.user_id=u.id
                JOIN roles AS r ON r.id=ur.role_id AND r.name='user'
                JOIN user_trading_controls AS utc ON utc.user_id=u.id
                JOIN mt5_accounts AS m ON m.owner_user_id=u.id
                JOIN mt5_account_approvals AS a
                  ON a.user_id=u.id
                 AND a.status='active'
                WHERE u.id=:user_id
                  AND u.status='active'
                  AND utc.trading_status='active'
                  AND m.status!='revoked'
                ORDER BY m.created_at DESC
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        ).mappings().first()

    def _rollback_account(self, user_id: UUID) -> tuple[str, str]:
        """Atomic rollback against the same approved member LIVE account."""
        with self._session_factory() as session:
            row = self._live_account_row(session, user_id)
        if row is None:
            raise Day26ExecutionError("day38_user_not_execution_ready")
        self._validate_live_account_row(row)
        try:
            token = self._cipher.decrypt(bytes(row["metaapi_token_ciphertext"])).strip()
        except BrokerCredentialDecryptionError as exc:
            raise Day26ExecutionError("broker_credential_decryption_failed") from exc
        if len(token) < 20:
            raise Day26ExecutionError("metaapi_platform_token_not_configured")
        return str(row["metaapi_account_id"]), token

    @staticmethod
    def _validate_live_account_row(row) -> None:  # noqa: ANN001
        if str(row["account_environment"] or "").lower() != "live":
            raise Day26ExecutionError("day38_live_account_required")
        if str(row["status"] or "") != "connected":
            raise Day26ExecutionError("mt5_account_not_connected")
        if str(row["login"] or "") != str(row["approved_login"] or ""):
            raise Day26ExecutionError("mt5_account_not_approved")
        if str(row["server"] or "").strip().lower() != str(
            row["approved_server"] or ""
        ).strip().lower():
            raise Day26ExecutionError("mt5_account_not_approved")


__all__ = ["Day38LiveUserExecutionService"]
