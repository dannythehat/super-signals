"""Day 38 listener wiring for independent Owner/member execution."""

from __future__ import annotations

import logging
import os
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.day28_zone_guard import Day28GuardedExecutionService
from app.day38_database_source_router import DatabaseSourceDay38FullExecutionRouter
from app.metaapi_margin_gateway import MetaApiMarginGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_execution_day38 import Day38LiveUserExecutionService
from app.mt5_management_day27 import Day27Mt5ManagementService
from app.mt5_management_day38 import Day38LiveUserManagementService
from app.multi_user_distribution_day38 import Day38MultiUserDistributionService
from app.multi_user_management_day38 import Day38MultiUserManagementService
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener_day28 import Day28TelegramListenerManager

logger = logging.getLogger(__name__)


def _enabled(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_day38_execution_router_from_env(
    *,
    session_factory: sessionmaker[Session],
) -> DatabaseSourceDay38FullExecutionRouter | None:
    """Build automatic execution using durable DB source status as source eligibility."""
    if not _enabled(os.getenv("SUPER_SIGNALS_DAY28_AUTO_EXECUTION_ENABLED")):
        return None
    try:
        owner_user_id = UUID(os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip())
    except ValueError:
        logger.error("Day 38 automatic execution disabled: invalid owner UUID configuration")
        return None

    broker_key_value = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    broker_keys = tuple(value.strip() for value in broker_key_value.split(",") if value.strip())
    if not broker_keys:
        logger.error("Day 38 automatic execution disabled: broker encryption keys are unavailable")
        return None

    risk_percent = os.getenv("SUPER_SIGNALS_DAY28_RISK_PERCENT", "1").strip() or "1"
    double_lot_approved = _enabled(
        os.getenv("SUPER_SIGNALS_DAY28_ALLOW_DOUBLE_LOT"),
        default=True,
    )

    try:
        cipher = MetaApiTokenCipher(broker_keys)
        read_gateway = MetaApiReadGateway()
        trade_gateway = MetaApiTradeGateway()
        margin_gateway = MetaApiMarginGateway()

        owner_execution = Day28GuardedExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            margin_gateway=margin_gateway,
            trade_gateway=trade_gateway,
        )
        owner_management = Day27Mt5ManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            trade_gateway=trade_gateway,
        )
        member_execution = Day38LiveUserExecutionService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            margin_gateway=margin_gateway,
            trade_gateway=trade_gateway,
        )
        member_management = Day38LiveUserManagementService(
            session_factory=session_factory,
            cipher=cipher,
            read_gateway=read_gateway,
            trade_gateway=trade_gateway,
        )
        return DatabaseSourceDay38FullExecutionRouter(
            session_factory=session_factory,
            owner_user_id=owner_user_id,
            execution_service=owner_execution,
            management_service=owner_management,
            member_distribution=Day38MultiUserDistributionService(
                session_factory=session_factory,
                execution_service=member_execution,
            ),
            member_management=Day38MultiUserManagementService(
                session_factory=session_factory,
                management_service=member_management,
            ),
            risk_percent=risk_percent,
            double_lot_approved=double_lot_approved,
        )
    except ValueError as exc:
        logger.error("Day 38 automatic execution disabled: %s", str(exc))
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
    """Preserve the proven Day 28 listener/catch-up mechanics with a Day 38 router."""
    return Day28TelegramListenerManager(
        api_id=api_id,
        api_hash=api_hash,
        cipher=cipher,
        session_factory=session_factory,
        refresh_seconds=refresh_seconds,
        excluded_chat_id=excluded_chat_id,
        day28_router=build_day38_execution_router_from_env(
            session_factory=session_factory,
        ),
    )


__all__ = ["build_day38_execution_router_from_env", "build_day38_listener_manager"]
