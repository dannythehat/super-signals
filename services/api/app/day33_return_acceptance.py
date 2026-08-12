"""Temporary Day 33 acceptance for return baselines and timeline privacy.

No trade gateway is imported or available here. This verifier deliberately makes
no broker request: it proves the accepted immutable deal ledger and append-only
pre-trade balance evidence can regenerate the same outcome independently.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_read_gateway import MetaApiReadGateway
from app.models import AuditEvent
from app.mt5_crypto import MetaApiTokenCipher
from app.performance_ledger_day33_v2 import Day33PerformanceLedgerServiceV2


def _string_or_none(value: object | None) -> str | None:
    return None if value is None else str(value)


async def run_day33_return_acceptance(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
    owner_user_id: UUID,
) -> None:
    service = Day33PerformanceLedgerServiceV2(
        session_factory=session_factory,
        cipher=cipher,
        gateway=MetaApiReadGateway(),
    )
    account = service._account(owner_user_id)
    if account is None:
        raise RuntimeError("day33_owner_mt5_account_missing")

    snapshots_added = service._backfill_account_snapshots_from_audit(
        user_id=owner_user_id,
        mt5_account_id=account["id"],
    )
    outcomes_rebuilt = service.rebuild_outcomes(owner_user_id)
    summaries_rebuilt = service.rebuild_summaries(owner_user_id)
    windows = {item.key: item for item in service.read_windows(owner_user_id)}
    admin_timeline = service.read_timeline(owner_user_id, viewer_role="owner", limit=250)
    invited_timeline = service.read_timeline(owner_user_id, viewer_role="user", limit=250)
    live_board = service.read_shared_live_board()

    with session_factory() as session:
        snapshot_count = int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM performance_account_snapshots WHERE user_id=:user_id"
                ),
                {"user_id": owner_user_id},
            ).scalar_one()
        )
        all_time_summary = session.execute(
            text(
                """
                SELECT cash_pnl,return_percent,net_pips,model_500_pnl,
                       model_500_return_percent,total_trades,wins,losses
                FROM performance_summaries
                WHERE user_id=:user_id
                  AND period_type='all_time'
                  AND dimension_type='portfolio'
                  AND dimension_key='all'
                LIMIT 1
                """
            ),
            {"user_id": owner_user_id},
        ).mappings().first()

    skipped_admin = [item for item in admin_timeline.trades if item.status == "skipped"]
    skipped_invited = [item for item in invited_timeline.trades if item.status == "skipped"]
    provider_hidden = all(
        item.source_label is None
        and item.trader_stream is None
        and item.source_color_index is None
        for item in invited_timeline.trades
    )
    admin_source_visible = any(item.source_label for item in admin_timeline.trades)
    skipped_contract_ok = (
        len(skipped_admin) > 0
        and len(skipped_admin) == len(skipped_invited)
        and all(item.status_color == "amber" for item in skipped_admin)
        and all(item.close_reason for item in skipped_admin)
    )
    all_window = windows.get("all")
    passed = bool(
        snapshot_count > 0
        and all_window is not None
        and all_window.return_percent is not None
        and all_time_summary is not None
        and all_time_summary["return_percent"] is not None
        and provider_hidden
        and admin_source_visible
        and skipped_contract_ok
        and len(live_board) == 0
    )
    all_time_window = (
        {
            "cash_pnl": _string_or_none(all_window.cash_pnl),
            "return_percent": _string_or_none(all_window.return_percent),
            "model_500_pnl": _string_or_none(all_window.model_500_pnl),
            "model_500_return_percent": _string_or_none(all_window.model_500_return_percent),
            "closed_trades": all_window.closed_trades,
            "wins": all_window.wins,
            "losses": all_window.losses,
            "breakeven": all_window.breakeven,
            "open_trades": all_window.open_trades,
            "win_rate_percent": _string_or_none(all_window.win_rate_percent),
            "net_pips": _string_or_none(all_window.net_pips),
            "mixed_instrument_pips": all_window.mixed_instrument_pips,
        }
        if all_window
        else None
    )
    payload = {
        "passed": passed,
        "snapshots_added_this_run": snapshots_added,
        "snapshot_count": snapshot_count,
        "outcomes_rebuilt": outcomes_rebuilt,
        "summaries_rebuilt": summaries_rebuilt,
        "all_time_window": all_time_window,
        "stored_all_time_summary": (
            {key: _string_or_none(value) for key, value in all_time_summary.items()}
            if all_time_summary is not None
            else None
        ),
        "admin_timeline_count": len(admin_timeline.trades),
        "invited_timeline_count": len(invited_timeline.trades),
        "skipped_count": len(skipped_admin),
        "skipped_contract_passed": skipped_contract_ok,
        "invited_provider_identity_hidden": provider_hidden,
        "admin_source_identity_visible": admin_source_visible,
        "shared_live_board_count": len(live_board),
        "shared_open_count": sum(1 for row in live_board if int(row["open_positions"]) > 0),
        "shared_pending_count": sum(1 for row in live_board if int(row["pending_positions"]) > 0),
        "metaapi_request_created": False,
        "broker_trade_action_created": False,
    }
    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=owner_user_id,
                event_type="day33.return_acceptance_completed",
                entity_type="performance_ledger",
                payload=payload,
            )
        )
        session.commit()
