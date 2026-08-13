"""Read-only Day 42 final GO / NO-GO gate.

The gate never sends an invitation and never calls a broker or Telegram gateway.
Owner approval and operational sign-off remain explicit external/manual decisions.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from sqlalchemy import text

from app.db import get_session_factory


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Day42FinalReadiness:
    go: bool
    ready_for_owner_go: bool
    blockers: tuple[str, ...]
    day40_regression_proven: bool
    day41_live_pilot_passed: bool
    prior_days_accepted: bool
    durable_database_verified: bool
    monitoring_support_verified: bool
    first_user_owner_approved: bool
    ordinary_members_inactive: bool
    global_emergency_absent: bool
    publication_failures_clear: bool
    owner_go_authorized: bool
    invitation_created: bool = False
    trade_action_created: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_day42_final_readiness(
    *,
    day40_regression_proven: bool,
    day41_live_pilot_passed: bool,
    durable_database_verified: bool,
    monitoring_support_verified: bool,
    first_user_owner_approved: bool,
    ordinary_members_inactive: bool,
    global_emergency_absent: bool,
    publication_failures_clear: bool,
    owner_go_authorized: bool,
) -> Day42FinalReadiness:
    prior_days_accepted = day40_regression_proven and day41_live_pilot_passed
    checks = (
        (prior_days_accepted, "prior_days_not_fully_accepted"),
        (durable_database_verified, "durable_database_not_verified"),
        (monitoring_support_verified, "monitoring_support_not_verified"),
        (first_user_owner_approved, "first_user_not_owner_approved"),
        (ordinary_members_inactive, "ordinary_member_automation_already_active"),
        (global_emergency_absent, "obsolete_global_emergency_control_present"),
        (publication_failures_clear, "publication_failures_present"),
    )
    blockers = tuple(reason for passed, reason in checks if not passed)
    ready_for_owner_go = not blockers
    go = ready_for_owner_go and owner_go_authorized
    if ready_for_owner_go and not owner_go_authorized:
        blockers = ("owner_final_go_not_authorized",)
    return Day42FinalReadiness(
        go=go,
        ready_for_owner_go=ready_for_owner_go,
        blockers=blockers,
        day40_regression_proven=day40_regression_proven,
        day41_live_pilot_passed=day41_live_pilot_passed,
        prior_days_accepted=prior_days_accepted,
        durable_database_verified=durable_database_verified,
        monitoring_support_verified=monitoring_support_verified,
        first_user_owner_approved=first_user_owner_approved,
        ordinary_members_inactive=ordinary_members_inactive,
        global_emergency_absent=global_emergency_absent,
        publication_failures_clear=publication_failures_clear,
        owner_go_authorized=owner_go_authorized,
    )


def read_day42_final_readiness() -> Day42FinalReadiness:
    with get_session_factory()() as session:
        day40 = int(
            session.scalar(
                text("SELECT count(*) FROM audit_events WHERE event_type='day40.database_regression_passed'")
            )
            or 0
        )
        day41 = int(
            session.scalar(
                text("SELECT count(*) FROM audit_events WHERE event_type='day41.owner_live_pilot_passed'")
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
        failed_publications = int(
            session.scalar(
                text("SELECT count(*) FROM telegram_publications WHERE lower(status)='failed'")
            )
            or 0
        )

    return evaluate_day42_final_readiness(
        day40_regression_proven=day40 > 0,
        day41_live_pilot_passed=day41 > 0,
        durable_database_verified=_flag("SUPER_SIGNALS_DAY41_DURABLE_DATABASE_VERIFIED"),
        monitoring_support_verified=_flag("SUPER_SIGNALS_DAY42_MONITORING_SUPPORT_VERIFIED"),
        first_user_owner_approved=_flag("SUPER_SIGNALS_DAY42_FIRST_USER_APPROVED"),
        ordinary_members_inactive=active_ordinary_members == 0,
        global_emergency_absent=emergency_permission == 0,
        publication_failures_clear=failed_publications == 0,
        owner_go_authorized=_flag("SUPER_SIGNALS_DAY42_GO_AUTHORIZED"),
    )


__all__ = [
    "Day42FinalReadiness",
    "evaluate_day42_final_readiness",
    "read_day42_final_readiness",
]
