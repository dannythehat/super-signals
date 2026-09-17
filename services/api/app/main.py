"""FastAPI application entry point."""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from uuid import UUID

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.aidy_decision_outcome_runtime import AidyDecisionOutcomeRuntime
from app.aidy_decision_runtime import AidyDecisionRuntime
from app.aidy_shadow_runtime import AidyShadowRuntime
from app.broker_settlement_canonical import CanonicalBrokerSettlementManager
from app.config import get_settings
from app.day26_code_acceptance import run_day26_code_acceptance_probe
from app.day27_code_acceptance import run_day27_code_acceptance_probe
from app.day34_code_acceptance import run_day34_code_acceptance_probe
from app.day34_live_acceptance import run_day34_live_acceptance_safely
from app.db import get_session_factory
from app.metaapi_gateway import MetaApiProvisioningGateway
from app.metaapi_read_gateway import MetaApiReadGateway
from app.metaapi_trade_gateway import MetaApiTradeGateway
from app.mt5_connection_manager import Mt5ConnectionManager
from app.mt5_connection_service import Mt5ConnectionError, Mt5DemoConnectionService
from app.mt5_connection_service_day30 import Day30Mt5ConnectionService
from app.mt5_crypto import MetaApiTokenCipher
from app.mt5_day23_acceptance import run_day23_acceptance_probe
from app.mt5_recovery import (
    probe_existing_metaapi_account,
    reencrypt_existing_metaapi_token,
    verify_existing_metaapi_token,
)
from app.performance_runtime import (
    CanonicalPerformanceRuntimeService as CanonicalPerformanceLedgerService,
)
from app.production_listener import build_production_listener_manager
from app.provider_trade_scoring_runtime import ProviderTradeScoringRuntime
from app.publisher_config import get_publisher_settings
from app.push_notifications_day34 import Day34PushNotificationManager
from app.routes.access import router as access_router
from app.routes.admin_accounts import router as admin_accounts_router
from app.routes.aidy_overview import router as aidy_overview_router
from app.routes.auth import router as auth_router
from app.routes.day26_execution import router as day26_execution_router
from app.routes.day27_management import router as day27_management_router
from app.routes.health import router as health_router
from app.routes.mt5_accounts import router as mt5_accounts_router
from app.routes.mt5_approvals_day30 import router as mt5_approvals_day30_router
from app.routes.notifications_day34 import router as notifications_day34_router
from app.routes.signals import router as signals_router
from app.routes.telegram_accounts import router as telegram_accounts_router
from app.routes.telegram_classifications import router as telegram_classifications_router
from app.routes.telegram_e2e_gate import router as telegram_e2e_gate_router
from app.routes.telegram_messages import router as telegram_messages_router
from app.routes.telegram_parses import router as telegram_parses_router
from app.routes.telegram_publisher import router as telegram_publisher_router
from app.routes.telegram_reliability import (
    provide_day14_telegram_source_service,
)
from app.routes.telegram_reliability import (
    router as telegram_reliability_router,
)
from app.routes.telegram_reviews import router as telegram_reviews_router
from app.routes.telegram_sources import (
    provide_telegram_source_service,
)
from app.routes.telegram_sources import (
    router as telegram_sources_router,
)
from app.routes.user_mt5_accounts import router as user_mt5_accounts_router
from app.shadow_trading import ShadowTradeManager
from app.telegram_crypto import TelegramSessionCipher
from app.telegram_listener import TelegramListenerManager
from app.telegram_publisher_canonical import CanonicalTelegramPublisherManager

logger = logging.getLogger(__name__)


async def _run_day22_mt5_bootstrap(service: Mt5DemoConnectionService) -> None:
    """One-time owner demo bootstrap using temporary Render secrets.

    Credential values are never logged. The connection service persists only the
    encrypted MetaAPI token and account metadata; the broker password is discarded.
    """
    if os.getenv("SUPER_SIGNALS_DAY22_BOOTSTRAP_ENABLED", "").strip() != "1":
        return

    owner_id_raw = os.getenv("SUPER_SIGNALS_DAY22_OWNER_ID", "").strip()
    metaapi_token = os.getenv("SUPER_SIGNALS_API", "").strip()
    login = os.getenv("SUPER_SIGNALS_DAY22_DEMO_LOGIN", "").strip()
    server = os.getenv("SUPER_SIGNALS_DAY22_DEMO_SERVER", "").strip()
    password = os.getenv("SUPER_SIGNALS_DAY22_DEMO_PASSWORD", "")

    if not all((owner_id_raw, metaapi_token, login, server, password)):
        logger.error(
            "Day 22 MT5 bootstrap missing config owner=%s token=%s login=%s server=%s password=%s",
            bool(owner_id_raw),
            bool(metaapi_token),
            bool(login),
            bool(server),
            bool(password),
        )
        return

    try:
        owner_user_id = UUID(owner_id_raw)
    except ValueError:
        logger.error("Day 22 MT5 bootstrap skipped: owner id is invalid")
        return

    try:
        view = await service.connect_owner_demo(
            owner_user_id=owner_user_id,
            metaapi_token=metaapi_token,
            login=login,
            password=password,
            server=server,
        )
    except Mt5ConnectionError as exc:
        logger.error("Day 22 MT5 bootstrap failed code=%s", exc.code)
        return
    except Exception:
        logger.exception("Day 22 MT5 bootstrap failed unexpectedly")
        return

    logger.info(
        "Day 22 MT5 bootstrap completed status=%s remote_state=%s remote_connection_status=%s",
        view.status,
        view.remote_state,
        view.remote_connection_status,
    )


@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    publisher_settings = get_publisher_settings()
    session_factory = get_session_factory()
    aidy_shadow_runtime = AidyShadowRuntime(session_factory)
    application.state.aidy_shadow_runtime = aidy_shadow_runtime
    await aidy_shadow_runtime.start()
    provider_scoring_runtime = ProviderTradeScoringRuntime(session_factory)
    application.state.provider_scoring_runtime = provider_scoring_runtime
    await provider_scoring_runtime.start()
    aidy_decision_runtime = AidyDecisionRuntime(session_factory)
    application.state.aidy_decision_runtime = aidy_decision_runtime
    await aidy_decision_runtime.start()
    aidy_decision_outcome_runtime = AidyDecisionOutcomeRuntime(session_factory)
    application.state.aidy_decision_outcome_runtime = aidy_decision_outcome_runtime
    await aidy_decision_outcome_runtime.start()

    if os.getenv("SUPER_SIGNALS_DAY26_CODE_PROBE", "").strip() == "1":
        await run_day26_code_acceptance_probe()
    if os.getenv("SUPER_SIGNALS_DAY27_CODE_PROBE", "").strip() == "1":
        await run_day27_code_acceptance_probe()
    if os.getenv("SUPER_SIGNALS_DAY34_CODE_PROBE", "").strip() == "1":
        run_day34_code_acceptance_probe()

    day34_reference_raw = (
        os.getenv("SUPER_SIGNALS_DAY34_REFERENCE_USER_ID", "").strip()
        or os.getenv("SUPER_SIGNALS_DAY28_OWNER_ID", "").strip()
        or os.getenv("SUPER_SIGNALS_DAY22_OWNER_ID", "").strip()
    )
    day34_reference_user_id: UUID | None = None
    if day34_reference_raw:
        try:
            day34_reference_user_id = UUID(day34_reference_raw)
        except ValueError:
            logger.error(
                "Day 34 reference user is invalid; shared summaries/settlement watch are disabled"
            )

    broker_key_value = (
        os.getenv("SUPER_SIGNALS_BROKER_CREDENTIAL_KEYS")
        or os.getenv("SUPER_SIGNALS_MT5_ENCRYPTION_KEYS")
        or ""
    )
    broker_keys = tuple(value.strip() for value in broker_key_value.split(",") if value.strip())
    mt5_connection_manager: Mt5ConnectionManager | None = None
    mt5_bootstrap_task: asyncio.Task[None] | None = None
    day34_settlement_manager: CanonicalBrokerSettlementManager | None = None
    shadow_trade_manager: ShadowTradeManager | None = None
    day34_live_acceptance_task: asyncio.Task[None] | None = None
    if broker_keys:
        broker_cipher = MetaApiTokenCipher(broker_keys)
        gateway = MetaApiProvisioningGateway()
        mt5_connection_service = Day30Mt5ConnectionService(
            session_factory=session_factory,
            cipher=broker_cipher,
            gateway=gateway,
        )
        application.state.mt5_connection_service = mt5_connection_service

        day33_performance_service = CanonicalPerformanceLedgerService(
            session_factory=session_factory,
            cipher=broker_cipher,
            gateway=MetaApiReadGateway(),
        )
        application.state.day33_performance_service = day33_performance_service

        if os.getenv("SUPER_SIGNALS_DAY34_SETTLEMENT_WATCH_ENABLED", "").strip() == "1":
            if day34_reference_user_id is None:
                logger.error(
                    "Day 34 settlement watch disabled: reference user is missing or invalid"
                )
            else:
                try:
                    poll_seconds = int(
                        os.getenv("SUPER_SIGNALS_DAY34_SETTLEMENT_POLL_SECONDS", "15").strip()
                        or "15"
                    )
                    day34_settlement_manager = CanonicalBrokerSettlementManager(
                        session_factory=session_factory,
                        performance_service=day33_performance_service,
                        reference_user_id=day34_reference_user_id,
                        poll_seconds=poll_seconds,
                        cipher=broker_cipher,
                        read_gateway=MetaApiReadGateway(),
                        trade_gateway=MetaApiTradeGateway(),
                    )
                    application.state.day34_settlement_manager = day34_settlement_manager
                except (ValueError, TypeError):
                    logger.error("Day 34 settlement watch disabled: poll interval is invalid")

        if day34_reference_user_id is not None:
            shadow_trade_manager = ShadowTradeManager(
                session_factory=session_factory, cipher=broker_cipher,
                gateway=MetaApiReadGateway(), owner_user_id=day34_reference_user_id,
                poll_seconds=int(os.getenv("SUPER_SIGNALS_SHADOW_POLL_SECONDS", "15") or "15"),
            )
            application.state.shadow_trade_manager = shadow_trade_manager

        allow_mt5_manager = True
        diagnostic_probe = os.getenv("SUPER_SIGNALS_DAY22_DIAGNOSTIC_PROBE", "").strip() == "1"
        if os.getenv("SUPER_SIGNALS_DAY22_REKEY_EXISTING_TOKEN", "").strip() == "1":
            allow_mt5_manager = False
            owner_id_raw = os.getenv("SUPER_SIGNALS_DAY22_OWNER_ID", "").strip()
            metaapi_token = os.getenv("SUPER_SIGNALS_API", "").strip()
            if not owner_id_raw or len(metaapi_token) < 20:
                logger.error(
                    "Day 22 MetaAPI token recovery skipped owner=%s token=%s",
                    bool(owner_id_raw),
                    len(metaapi_token) >= 20,
                )
            else:
                try:
                    owner_user_id = UUID(owner_id_raw)
                    recovered = reencrypt_existing_metaapi_token(
                        session_factory=session_factory,
                        cipher=broker_cipher,
                        owner_user_id=owner_user_id,
                        metaapi_token=metaapi_token,
                    )
                    verified = recovered and verify_existing_metaapi_token(
                        session_factory=session_factory,
                        cipher=broker_cipher,
                        owner_user_id=owner_user_id,
                        expected_token=metaapi_token,
                    )
                    allow_mt5_manager = verified
                    if verified and diagnostic_probe:
                        await probe_existing_metaapi_account(
                            session_factory=session_factory,
                            gateway=gateway,
                            owner_user_id=owner_user_id,
                            metaapi_token=metaapi_token,
                        )
                        allow_mt5_manager = False
                    logger.info(
                        "Day 22 MetaAPI token recovery completed existing_account=%s local_verification=%s diagnostic_probe=%s",
                        recovered,
                        verified,
                        diagnostic_probe,
                    )
                except (ValueError, RuntimeError):
                    logger.exception("Day 22 MetaAPI token recovery failed safely")

        if allow_mt5_manager:
            mt5_connection_manager = Mt5ConnectionManager(mt5_connection_service)
            await mt5_connection_manager.start()
        elif not diagnostic_probe:
            logger.error(
                "Day 22 MT5 reconciliation suppressed because local token verification did not pass"
            )

        mt5_bootstrap_task = asyncio.create_task(
            _run_day22_mt5_bootstrap(mt5_connection_service),
            name="super-signals-day22-mt5-bootstrap",
        )
        await run_day23_acceptance_probe(
            session_factory=session_factory,
            cipher=broker_cipher,
        )

    push_manager: Day34PushNotificationManager | None = None
    vapid_private_key = os.getenv("SUPER_SIGNALS_WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
    vapid_subject = os.getenv("SUPER_SIGNALS_WEB_PUSH_VAPID_SUBJECT", "").strip()
    if vapid_private_key and vapid_subject:
        try:
            push_poll_seconds = int(
                os.getenv("SUPER_SIGNALS_DAY34_PUSH_POLL_SECONDS", "3").strip() or "3"
            )
            push_manager = Day34PushNotificationManager(
                session_factory=session_factory,
                vapid_private_key=vapid_private_key,
                vapid_subject=vapid_subject,
                poll_seconds=push_poll_seconds,
            )
            application.state.day34_push_manager = push_manager
        except (ValueError, TypeError):
            logger.error("Day 34 Web Push disabled: VAPID or poll configuration is invalid")

    publisher_destination_excluded = bool(
        publisher_settings.enabled and publisher_settings.destination_chat_id is not None
    )

    publisher = CanonicalTelegramPublisherManager(
        session_factory=session_factory,
        enabled=publisher_settings.enabled,
        bot_token=publisher_settings.bot_token,
        destination_chat_id=publisher_settings.destination_chat_id,
        poll_seconds=publisher_settings.poll_seconds,
        reader_exclusion_active=publisher_destination_excluded,
        reference_user_id=day34_reference_user_id,
    )
    application.state.telegram_publisher = publisher

    listener: TelegramListenerManager | None = None
    if (
        settings.telegram_listener_enabled
        and settings.telegram_api_id is not None
        and settings.telegram_api_hash is not None
    ):
        listener = build_production_listener_manager(
            api_id=settings.telegram_api_id,
            api_hash=settings.telegram_api_hash,
            cipher=TelegramSessionCipher(settings.telegram_session_keys),
            session_factory=session_factory,
            refresh_seconds=settings.telegram_listener_refresh_seconds,
            excluded_chat_id=(
                publisher_settings.destination_chat_id
                if publisher_destination_excluded
                else None
            ),
        )
        application.state.telegram_listener = listener
        await listener.start()

    if day34_settlement_manager is not None:
        await day34_settlement_manager.start()
    if shadow_trade_manager is not None:
        await shadow_trade_manager.start()
    if push_manager is not None:
        await push_manager.start()
    await publisher.start()

    if os.getenv("SUPER_SIGNALS_DAY34_LIVE_ACCEPTANCE", "").strip() == "1":
        day34_live_acceptance_task = asyncio.create_task(
            run_day34_live_acceptance_safely(
                settlement_manager=day34_settlement_manager,
            ),
            name="day34-live-acceptance",
        )

    try:
        yield
    finally:
        if day34_live_acceptance_task is not None:
            if not day34_live_acceptance_task.done():
                day34_live_acceptance_task.cancel()
            try:
                await day34_live_acceptance_task
            except asyncio.CancelledError:
                pass
        await aidy_decision_outcome_runtime.stop()
        await aidy_decision_runtime.stop()
        await provider_scoring_runtime.stop()
        await aidy_shadow_runtime.stop()
        await publisher.stop()
        if push_manager is not None:
            await push_manager.stop()
        if shadow_trade_manager is not None:
            await shadow_trade_manager.stop()
        if day34_settlement_manager is not None:
            await day34_settlement_manager.stop()
        if listener is not None:
            await listener.stop()
        if mt5_bootstrap_task is not None and not mt5_bootstrap_task.done():
            mt5_bootstrap_task.cancel()
            try:
                await mt5_bootstrap_task
            except asyncio.CancelledError:
                pass
        if mt5_connection_manager is not None:
            await mt5_connection_manager.stop()


def _mount_web_application(application: FastAPI) -> None:
    web_dist_value = os.getenv("SUPER_SIGNALS_WEB_DIST")
    if not web_dist_value:

        @application.get("/", include_in_schema=False)
        def root() -> dict[str, str]:
            return {"name": "Super Signals API", "docs": "/docs"}

        return

    web_dist = Path(web_dist_value)
    if not web_dist.is_dir():
        raise RuntimeError(f"SUPER_SIGNALS_WEB_DIST does not exist: {web_dist}")
    application.mount("/", StaticFiles(directory=web_dist, html=True), name="web")


def _configure_logging() -> None:
    """Make the application's own diagnostics visible in the platform log.

    Uvicorn configures its own loggers with propagate disabled, so adding a root handler
    here does not duplicate access/server lines. Existing call sites log sanitized
    values only; this changes visibility, not content.
    """
    level_name = os.getenv("SUPER_SIGNALS_LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, "_super_signals", False) for h in root.handlers):
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(levelname)s:     %(name)s: %(message)s")
        )
        handler._super_signals = True  # type: ignore[attr-defined]
        root.addHandler(handler)


def create_app() -> FastAPI:
    _configure_logging()
    settings = get_settings()
    application = FastAPI(
        title="Super Signals API",
        version="1.0.0",
        description="Private signal-processing and trading infrastructure.",
        lifespan=_lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Accept", "Content-Type", "X-Request-ID"],
    )
    application.dependency_overrides[provide_telegram_source_service] = (
        provide_day14_telegram_source_service
    )
    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(access_router)
    application.include_router(admin_accounts_router)
    application.include_router(aidy_overview_router)
    application.include_router(telegram_accounts_router)
    application.include_router(telegram_sources_router)
    application.include_router(telegram_reliability_router)
    application.include_router(telegram_messages_router)
    application.include_router(telegram_classifications_router)
    application.include_router(telegram_parses_router)
    application.include_router(telegram_reviews_router)
    application.include_router(signals_router)
    application.include_router(telegram_publisher_router)
    application.include_router(telegram_e2e_gate_router)
    application.include_router(mt5_accounts_router)
    application.include_router(mt5_approvals_day30_router)
    application.include_router(user_mt5_accounts_router)
    application.include_router(day26_execution_router)
    application.include_router(day27_management_router)
    application.include_router(notifications_day34_router)
    _mount_web_application(application)
    return application


app = create_app()
