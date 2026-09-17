"""Canonical production execution-router wiring.

This is the only production builder for provider decisions -> paper/LIVE broker routing.
Each user has exactly one canonical active MT5 slot. The selected slot may be Vantage Demo
or Vantage Live; interpretation, risk, pending and management policy remain shared.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.active_account_execution import ActiveAccountCanonicalTradingExecutionService
from app.active_account_management import ActiveAccountCanonicalTradingManagementService
from app.active_account_member_routing import (
    ActiveAccountMemberDistributionService,
    ActiveAccountMemberManagementService,
)
from app.collective_execution_dispatch import CollectiveAwareCanonicalExecutionDispatcher
from app.execution_dispatch_canonical import CanonicalExecutionDispatcher
from app.graceful_market_targets import GracefulCaptureReliableMemberTradingExecutionService
from app.management_reliability_runtime import ManagementReliabilityRuntime
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.paper_resilient_read_gateway import PaperResilientMetaApiReadGateway
from app.trading_management_canonical import MemberTradingManagementService
from app.unified_pending_reconciler import UnifiedPendingReconciler

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


def build_canonical_execution_router(
    *,
    session_factory: sessionmaker[Session],
) -> CanonicalExecutionDispatcher | None:
    if not _enabled(os.getenv("SUPER_SIGNALS_DAY28_AUTO_EXECUTION_ENABLED")):
        return None
    try:
        owner_user_id = UUID(os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip())
    except ValueError:
        logger.error("Canonical execution disabled: invalid owner UUID configuration")
        return None

    broker_keys = _broker_keys()
    if not broker_keys:
        logger.error("Canonical execution disabled: broker encryption keys unavailable")
        return None

    risk_percent = os.getenv("SUPER_SIGNALS_DAY28_RISK_PERCENT", "1").strip() or "1"
    double_lot_approved = _enabled(
        os.getenv("SUPER_SIGNALS_DAY28_ALLOW_DOUBLE_LOT"),
        default=True,
    )

    try:
        cipher = MetaApiTokenCipher(broker_keys)
        owner_read = PaperResilientMetaApiReadGateway()
        member_read = PaperResilientMetaApiReadGateway()
        trade = MetaApiTradeGateway()
        margin = MetaApiMarginGateway()

        owner_execution = ActiveAccountCanonicalTradingExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=owner_read,
            margin_gateway=margin,
            trade_gateway=trade,
        )
        owner_management = ActiveAccountCanonicalTradingManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=owner_read,
            trade_gateway=trade,
        )

        # Ordinary member Demo/Paper execution uses the same canonical policy as the
        # reference account, but with the member's own active account and risk controls.
        demo_member_execution = ActiveAccountCanonicalTradingExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read,
            margin_gateway=margin,
            trade_gateway=trade,
        )
        demo_member_management = ActiveAccountCanonicalTradingManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read,
            trade_gateway=trade,
        )

        # Real member execution retains its additional user-role/account-approval gate.
        live_member_execution = GracefulCaptureReliableMemberTradingExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read,
            margin_gateway=margin,
            trade_gateway=trade,
        )
        live_member_management = MemberTradingManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read,
            trade_gateway=trade,
        )

        return CollectiveAwareCanonicalExecutionDispatcher(
            session_factory=session_factory,
            owner_user_id=owner_user_id,
            execution_service=owner_execution,
            management_service=owner_management,
            member_distribution=ActiveAccountMemberDistributionService(
                session_factory=session_factory,
                demo_execution_service=demo_member_execution,
                live_execution_service=live_member_execution,
            ),
            member_management=ActiveAccountMemberManagementService(
                session_factory=session_factory,
                demo_management_service=demo_member_management,
                live_management_service=live_member_management,
            ),
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )
    except ValueError as exc:
        logger.error("Canonical execution disabled: %s", str(exc))
        return None


def build_management_reliability_runtime(
    *,
    session_factory: sessionmaker[Session],
    router: CanonicalExecutionDispatcher | None,
) -> ManagementReliabilityRuntime | None:
    """Retry, then fail-safe close, any provider management instruction that gets stuck.

    Built from the same credentials/config as the router itself so a stuck management
    instruction is recovered on whichever account -- owner demo, a member's own demo, or
    a member's real live account -- actually still holds the exposure.
    """
    if router is None:
        return None
    try:
        owner_user_id = UUID(os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip())
    except ValueError:
        return None
    broker_keys = _broker_keys()
    if not broker_keys:
        return None
    interval_seconds = int(
        os.getenv("SUPER_SIGNALS_MANAGEMENT_RELIABILITY_INTERVAL_SECONDS", "180").strip()
        or "180"
    )
    try:
        cipher = MetaApiTokenCipher(broker_keys)
        read_gateway = PaperResilientMetaApiReadGateway()
        trade = MetaApiTradeGateway()
        demo_management = ActiveAccountCanonicalTradingManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            trade_gateway=trade,
        )
        live_management = MemberTradingManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            trade_gateway=trade,
        )
        return ManagementReliabilityRuntime(
            session_factory=session_factory,
            dispatcher=router,
            demo_management=demo_management,
            live_management=live_management,
            owner_user_id=owner_user_id,
            interval_seconds=interval_seconds,
        )
    except ValueError:
        logger.error("Management reliability runtime disabled: invalid configuration")
        return None


def build_canonical_pending_reconciler(
    *,
    session_factory: sessionmaker[Session],
    router: CanonicalExecutionDispatcher | None,
) -> UnifiedPendingReconciler | None:
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
        return UnifiedPendingReconciler(
            session_factory=session_factory,
            cipher=MetaApiTokenCipher(broker_keys),
            gateway=PaperResilientMetaApiReadGateway(),
            trade_gateway=MetaApiTradeGateway(),
            owner_user_id=owner_user_id,
            poll_seconds=poll_seconds,
        )
    except (ValueError, TypeError):
        logger.error("Canonical pending reconciler disabled: invalid configuration")
        return None


__all__ = [
    "build_canonical_execution_router",
    "build_canonical_pending_reconciler",
    "build_management_reliability_runtime",
]
