"""Day 38 listener wiring for independent Owner/member execution."""

from __future__ import annotations

import logging
import os
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.day38_database_source_router import DatabaseSourceDay38FullExecutionRouter
from app.literal_management_overrides import install_literal_management_overrides
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_execution_day38 import Day38LiveUserExecutionService
from app.mt5_management_day38 import Day38LiveUserManagementService
from app.paper_critical_management_v2 import PaperCriticalManagementV2
from app.paper_fresh_run_reset import install_paper_fresh_run_reset
from app.paper_fresh_start_execution import PaperFreshStartExecutionService
from app.paper_pending_reconciler import PaperPendingReconciler
from app.paper_resilient_read_gateway import PaperResilientMetaApiReadGateway
from app.paper_safe_member_routing import (
    PaperSafeMemberDistribution,
    PaperSafeMemberManagement,
)
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener_day28 import Day28TelegramListenerManager

logger = logging.getLogger(__name__)


def _enabled(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _broker_keys() -> tuple[str, ...]:
    raw = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def build_day38_execution_router_from_env(
    *,
    session_factory: sessionmaker[Session],
) -> DatabaseSourceDay38FullExecutionRouter | None:
    """Build automatic execution using durable DB source status as source eligibility."""
    # A paper reset is a read-only display boundary and must remain active even when
    # automatic execution is temporarily disabled while a fresh demo account is linked.
    install_paper_fresh_run_reset()

    if not _enabled(os.getenv("SUPER_SIGNALS_DAY28_AUTO_EXECUTION_ENABLED")):
        return None
    try:
        owner_user_id = UUID(os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip())
    except ValueError:
        logger.error("Day 38 automatic execution disabled: invalid owner UUID configuration")
        return None

    broker_keys = _broker_keys()
    if not broker_keys:
        logger.error("Day 38 automatic execution disabled: broker encryption keys are unavailable")
        return None

    risk_percent = os.getenv("SUPER_SIGNALS_DAY28_RISK_PERCENT", "1").strip() or "1"
    double_lot_approved = _enabled(
        os.getenv("SUPER_SIGNALS_DAY28_ALLOW_DOUBLE_LOT"),
        default=True,
    )

    try:
        # Install mechanical corrections before any Telegram decision is routed. This
        # makes literal instructions such as "Move your SL back to entry" actionable
        # even when the same message also reports that TPs were hit.
        install_literal_management_overrides()

        cipher = MetaApiTokenCipher(broker_keys)
        owner_read_gateway = PaperResilientMetaApiReadGateway()
        member_read_gateway = MetaApiReadGateway()
        trade_gateway = MetaApiTradeGateway()
        margin_gateway = MetaApiMarginGateway()

        # Owner DEMO paper execution prioritises actually exercising provider trades:
        # fresh market instructions use the current broker price, broker-minimum risk
        # overruns do not veto a paper trade, retryable reads are retried briefly, and
        # layered structures use a minimal atomic allocation instead of entry x TP
        # Cartesian multiplication. LIVE member execution remains unchanged.
        owner_execution = PaperFreshStartExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=owner_read_gateway,
            margin_gateway=margin_gateway,
            trade_gateway=trade_gateway,
        )
        owner_management = PaperCriticalManagementV2(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=owner_read_gateway,
            trade_gateway=trade_gateway,
        )
        member_execution = Day38LiveUserExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read_gateway,
            margin_gateway=margin_gateway,
            trade_gateway=trade_gateway,
        )
        member_management = Day38LiveUserManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read_gateway,
            trade_gateway=trade_gateway,
        )
        return DatabaseSourceDay38FullExecutionRouter(
            session_factory=session_factory,
            owner_user_id=owner_user_id,
            execution_service=owner_execution,
            management_service=owner_management,
            member_distribution=PaperSafeMemberDistribution(
                session_factory=session_factory,
                execution_service=member_execution,
            ),
            member_management=PaperSafeMemberManagement(
                session_factory=session_factory,
                management_service=member_management,
            ),
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )
    except ValueError as exc:
        logger.error("Day 38 automatic execution disabled: %s", str(exc))
        return None


class PaperPendingAwareListenerManager(Day28TelegramListenerManager):
    """Run broker pending-fill observation alongside the proven Telegram reader."""

    def __init__(self, *, pending_reconciler: PaperPendingReconciler | None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._paper_pending_reconciler = pending_reconciler

    async def start(self) -> None:
        if self._paper_pending_reconciler is not None:
            await self._paper_pending_reconciler.start()
        try:
            await super().start()
        except Exception:
            if self._paper_pending_reconciler is not None:
                await self._paper_pending_reconciler.stop()
            raise

    async def stop(self) -> None:
        try:
            await super().stop()
        finally:
            if self._paper_pending_reconciler is not None:
                await self._paper_pending_reconciler.stop()


def _build_pending_reconciler(
    *,
    session_factory: sessionmaker[Session],
    router: DatabaseSourceDay38FullExecutionRouter | None,
) -> PaperPendingReconciler | None:
    if router is None:
        return None
    try:
        owner_user_id = UUID(os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip())
        poll_seconds = int(
            os.getenv("SUPER_SIGNALS_PAPER_PENDING_POLL_SECONDS", "3").strip() or "3"
        )
        broker_keys = _broker_keys()
        if not broker_keys:
            return None
        return PaperPendingReconciler(
            session_factory=session_factory,
            cipher=MetaApiTokenCipher(broker_keys),
            gateway=PaperResilientMetaApiReadGateway(),
            owner_user_id=owner_user_id,
            poll_seconds=poll_seconds,
        )
    except (ValueError, TypeError):
        logger.error("Paper pending reconciler disabled: invalid owner/key/poll configuration")
        return None


def build_day38_listener_manager(
    *,
    api_id: int,
    api_hash: str,
    cipher: TelegramSessionCipher,
    session_factory: sessionmaker[Session],
    refresh_seconds: int,
    excluded_chat_id: int | None,
) -> Day28TelegramListenerManager:
    """Preserve Day 28 mechanics and observe broker-held pending fills in paper mode."""
    router = build_day38_execution_router_from_env(session_factory=session_factory)
    return PaperPendingAwareListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
        excluded_chat_id=excluded_chat_id,
        day28_router=router,
        pending_reconciler=_build_pending_reconciler(
            session_factory=session_factory,
            router=router,
        ),
    )


__all__ = [
    "PaperPendingAwareListenerManager",
    "build_day38_execution_router_from_env",
    "build_day38_listener_manager",
]
