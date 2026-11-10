"""Broker gateway factory. Contabo-hosted MT5 local bridge is the only live transport."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session, sessionmaker

from app.local_bridge_gateways import (
    LocalBridgeMarginGateway,
    LocalBridgeReadGateway,
    LocalBridgeTradeGateway,
)
from app.local_bridge_queue import LocalBridgeQueue


@dataclass(frozen=True, slots=True)
class BrokerGateways:
    read: LocalBridgeReadGateway
    trade: LocalBridgeTradeGateway
    margin: LocalBridgeMarginGateway


def broker_transport() -> str:
    """Never instantiate the retired third-party MetaAPI broker transport.

    Even an obsolete SUPER_SIGNALS_BROKER_TRANSPORT=metaapi variable cannot
    reactivate remote execution. All broker I/O goes through the local MT5 bridge.
    """
    return "local_bridge"


def build_broker_gateways(
    session_factory: sessionmaker[Session],
    *,
    resilient_reads: bool = True,
    timeout_seconds: float | None = None,
) -> BrokerGateways:
    # No MetaAPI fallback, including when a local bridge is unavailable.
    profile = os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_PROFILE", "super-signals").strip()
    command_timeout = float(os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_COMMAND_TIMEOUT_SECONDS", "30"))
    queue = LocalBridgeQueue(
        session_factory,
        profile=profile,
        timeout_seconds=timeout_seconds or command_timeout,
    )
    raw_baseline = os.getenv("SUPER_SIGNALS_LOCAL_BRIDGE_RAW_BALANCE_BASELINE", "").strip()
    canonical_baseline = os.getenv(
        "SUPER_SIGNALS_LOCAL_BRIDGE_CANONICAL_BALANCE_BASELINE", ""
    ).strip()
    if bool(raw_baseline) != bool(canonical_baseline):
        raise ValueError("local_bridge_balance_baselines_incomplete")
    try:
        parsed_raw_baseline = Decimal(raw_baseline) if raw_baseline else None
        parsed_canonical_baseline = Decimal(canonical_baseline) if canonical_baseline else None
    except InvalidOperation as exc:
        raise ValueError("local_bridge_balance_baselines_invalid") from exc
    read = LocalBridgeReadGateway(
        queue,
        raw_balance_baseline=parsed_raw_baseline,
        canonical_balance_baseline=parsed_canonical_baseline,
    )
    return BrokerGateways(
        read=read,
        trade=LocalBridgeTradeGateway(queue, read_gateway=read),
        margin=LocalBridgeMarginGateway(queue),
    )


__all__ = ["BrokerGateways", "broker_transport", "build_broker_gateways"]
