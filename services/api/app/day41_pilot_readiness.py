"""Read-only fail-closed readiness gate for the Day 41 Owner LIVE pilot.

This module never calls Telegram or a broker gateway and never changes trading state.
External/manual approvals default to false unless explicitly configured by the Owner.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import text

from app.db import get_session_factory

_ALLOWED_RISKS = {Decimal("0.5"), Decimal("1"), Decimal("1.5"), Decimal("2")}
_ACCEPTED_MIGRATIONS = {"0024_day40_no_global_stop"}


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Day41PilotReadiness:
    ready: bool
    armed: bool
    blockers: tuple[str, ...]
    migration_ok: bool
    day40_regression_proven: bool
    canonical_owner_ok: bool
    owner_live_mt5_ok: bool
    owner_live_approval_ok: bool
    owner_risk_ok: bool
    owner_safe_start_ok: bool
    owner_mapped_exposure_clear: bool
    ordinary_members_inactive: bool
    global_emergency_absent: bool
    durable_database_verified: bool
    owner_limits_approved: bool
    trade_action_created: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_day41_pilot_readiness(
    *,
    migration_ok: bool,
    day40_regression_proven: bool,
    canonical_owner_ok: bool,
    owner_live_mt5_ok: bool,
    owner_live_approval_ok: bool,
    owner_risk_ok: bool,
    owner_safe_start_ok: bool,
    owner_mapped_exposure_clear: bool,
    ordinary_members_inactive: bool,
    global_emergency_absent: bool,
    durable_database_verified: bool,
    owner_limits_approved: bool,
    pilot_armed: bool,
) -> Day41PilotReadiness:
    checks = (
        (migration_ok, "accepted_migration_missing"),
        (day40_regression_proven, "day40_regression_evidence_missing"),
        (canonical_owner_ok, "canonical_owner_not_unique"),
        (owner_live_mt5_ok, "owner_live_mt5_not_connected"),
        (owner_live_approval_ok, "owner_live_mt5_not_approved"),
        (owner_risk_ok, "owner_live_risk_not_configured"),
        (owner_safe_start_ok, "owner_automation_not_stopped_for_preflight"),
        (owner_mapped_exposure_clear, "owner_mapped_exposure_not_clear"),
        (ordinary_members_inactive, "ordinary_member_automation_active"),
        (global_emergency_absent, "obsolete_global_emergency_control_present"),
        (durable_database_verified, "durable_database_not_verified"),
        (owner_limits_approved, "owner_live_limits_not_approved"),
    )
    blockers = tuple(reason for passed, reason in checks if not passed)
    technically_ready = not blockers
    armed = technically_ready and pilot_armed
    if technically_ready and not pilot_armed:
        blockers = ("owner_live_pilot_not_armed",)
    return Day41PilotReadiness(
        ready=technically_ready and pilot_armed,
        armed=armed,
        blockers=blockers,
        migration_ok=migration_ok,
        day40_regression_proven=day40_regression_proven,
        canonical_owner_ok=canonical_owner_ok,
        owner_live_mt5_ok=owner_live_mt5_ok,
        owner_live_approval_ok=owner_live_approval_ok,
        owner_risk_ok=owner_risk_ok,
        owner_safe_start_ok=owner_safe_start_ok,
        owner_mapped_exposure_clear=owner_mapped_exposure_clear,
        ordinary_members_inactive=ordinary_members_inactive,
        global_emergency_absent=global_emergency_absent,
        durable_database_verified=durable_database_verified,
        owner_limits_approved=owner_limits_approved,
    )


def read_day41_pilot_readiness() -> Day41PilotReadiness:
    session_factory = get_session_factory()
    with session_factory() as session:
        migration = session.scalar(text("SELECT version_num FROM alembic_version"))
        day40_evidence = int(
            session.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE event_type='day40.database_regression_passed'"
                )
            )
            or 0
        )
        owners = list(
            session.execute(
                text(
                    """
                    SELECT DISTINCT u.id
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN roles r ON r.id=ur.role_id
                    WHERE u.status='active' AND r.name='owner'
                    """
                )
            ).scalars()
        )
        owner_id = owners[0] if len(owners) == 1 else None

        live_accounts = 0
        live_approvals = 0
        owner_risk_ok = False
        owner_safe_start_ok = False
        owner_exposure = 0
        if owner_id is not None:
            live_accounts = int(
                session.scalar(
                    text(
                        """
                        SELECT count(*) FROM mt5_accounts
                        WHERE owner_user_id=:owner_id
                          AND lower(account_environment)='live'
                          AND lower(status)='connected'
                          AND upper(coalesce(remote_state,''))='DEPLOYED'
                          AND upper(coalesce(remote_connection_status,''))='CONNECTED'
                        """
                    ),
                    {"owner_id": owner_id},
                )
                or 0
            )
            live_approvals = int(
                session.scalar(
                    text(
                        """
                        SELECT count(*)
                        FROM mt5_account_approvals a
                        JOIN mt5_accounts m
                          ON m.owner_user_id=a.user_id
                         AND m.login=a.login
                         AND m.server=a.server
                         AND lower(m.account_environment)='live'
                        WHERE a.user_id=:owner_id
                          AND lower(a.account_environment)='live'
                          AND lower(a.status)='approved'
                          AND lower(m.status)='connected'
                        """
                    ),
                    {"owner_id": owner_id},
                )
                or 0
            )
            control = session.execute(
                text(
                    """
                    SELECT risk_percent, allow_double_lot, trading_status
                    FROM user_trading_controls WHERE user_id=:owner_id
                    """
                ),
                {"owner_id": owner_id},
            ).first()
            if control is not None:
                risk = Decimal(str(control.risk_percent)) if control.risk_percent is not None else None
                owner_risk_ok = risk in _ALLOWED_RISKS and control.allow_double_lot is not None
                owner_safe_start_ok = str(control.trading_status or "").lower() == "stopped"
            owner_exposure = int(
                session.scalar(
                    text(
                        """
                        SELECT count(*) FROM positions
                        WHERE user_id=:owner_id
                          AND broker_position_id IS NOT NULL
                          AND lower(status) IN ('open','pending')
                        """
                    ),
                    {"owner_id": owner_id},
                )
                or 0
            )

        active_ordinary_members = int(
            session.scalar(
                text(
                    """
                    SELECT count(DISTINCT u.id)
                    FROM users u
                    JOIN user_roles ur ON ur.user_id=u.id
                    JOIN roles r ON r.id=ur.role_id AND r.name='user'
                    JOIN user_trading_controls tc ON tc.user_id=u.id
                    WHERE u.status='active'
                      AND lower(tc.trading_status)='active'
                      AND NOT EXISTS (
                          SELECT 1 FROM user_roles our
                          JOIN roles ro ON ro.id=our.role_id
                          WHERE our.user_id=u.id AND ro.name='owner'
                      )
                    """
                )
            )
            or 0
        )
        emergency_permission = int(
            session.scalar(
                text("SELECT count(*) FROM permissions WHERE code='emergency_stop.use'")
            )
            or 0
        )

    return evaluate_day41_pilot_readiness(
        migration_ok=str(migration) in _ACCEPTED_MIGRATIONS,
        day40_regression_proven=day40_evidence > 0,
        canonical_owner_ok=owner_id is not None,
        owner_live_mt5_ok=live_accounts == 1,
        owner_live_approval_ok=live_approvals == 1,
        owner_risk_ok=owner_risk_ok,
        owner_safe_start_ok=owner_safe_start_ok,
        owner_mapped_exposure_clear=owner_exposure == 0,
        ordinary_members_inactive=active_ordinary_members == 0,
        global_emergency_absent=emergency_permission == 0,
        durable_database_verified=_flag("SUPER_SIGNALS_DAY41_DURABLE_DATABASE_VERIFIED"),
        owner_limits_approved=_flag("SUPER_SIGNALS_DAY41_OWNER_LIMITS_APPROVED"),
        pilot_armed=_flag("SUPER_SIGNALS_DAY41_LIVE_PILOT_ARMED"),
    )


__all__ = [
    "Day41PilotReadiness",
    "evaluate_day41_pilot_readiness",
    "read_day41_pilot_readiness",
]
