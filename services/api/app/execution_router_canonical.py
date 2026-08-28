"""Canonical production execution-router wiring.

This is the only production builder for provider decisions -> paper/future-LIVE broker
routing. It installs no runtime patches and no day-numbered router generation. Paper and
future LIVE use the same execution, management, transient-capture retry and pending-fill
reconciliation policy; only account eligibility/credentials and the explicit member-
distribution switch differ.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.execution_capture_reliability import (
    CaptureReliableCanonicalTradingExecutionService,
    CaptureReliableMemberTradingExecutionService,
)
from app.execution_dispatch_canonical import CanonicalExecutionDispatcher
from app.member_routing_canonical import MemberDistributionService, MemberManagementService
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.paper_resilient_read_gateway import PaperResilientMetaApiReadGateway
from app.trading_management_canonical import (
    CanonicalTradingManagementService,
    MemberTradingManagementService,
)
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
        member_read = MetaApiReadGateway()
        trade = MetaApiTradeGateway()
        # This dependency remains constructor-compatible with the lower execution
        # transport, but canonical policy never uses local margin availability as an
        # approval/veto budget. Vantage/MT5 is authoritative for actual rejection.
        margin = MetaApiMarginGateway()

        owner_execution = CaptureReliableCanonicalTradingExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=owner_read,
            margin_gateway=margin,
            trade_gateway=trade,
        )
        owner_management = CanonicalTradingManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=owner_read,
            trade_gateway=trade,
        )
        member_execution = CaptureReliableMemberTradingExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read,
            margin_gateway=margin,
            trade_gateway=trade,
        )
        member_management = MemberTradingManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=member_read,
            trade_gateway=trade,
        )
        return CanonicalExecutionDispatcher(
            session_factory=session_factory,
            owner_user_id=owner_user_id,
            execution_service=owner_execution,
            management_service=owner_management,
            member_distribution=MemberDistributionService(
                session_factory=session_factory,
                execution_service=member_execution,
            ),
            member_management=MemberManagementService(
                session_factory=session_factory,
                management_service=member_management,
            ),
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )
    except ValueError as exc:
        logger.error("Canonical execution disabled: %s", str(exc))
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


__all__ = ["build_canonical_execution_router", "build_canonical_pending_reconciler"]
