"""Temporary Day 31 acceptance against Render Postgres with fake broker gateways."""

from __future__ import annotations

import logging
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text

from app.models import AuditEvent
from app.trading_controls_day31 import Day31TradingControlService

logger = logging.getLogger(__name__)


class _FakeReadGateway:
    def __init__(self, positions: list[dict[str, object]]) -> None:
        self.positions = positions
        self.read_calls = 0

    async def resolve_account_region(self, *, token: str, account_id: str) -> str:
        if not token or not account_id:
            raise AssertionError("missing encrypted broker runtime material")
        return "day31-fake-region"

    async def read_positions(self, *, token: str, account_id: str, region: str):
        del token, account_id, region
        self.read_calls += 1
        return list(self.positions)


class _FakeTradeGateway:
    def __init__(self) -> None:
        self.closed: list[str] = []

    async def close_position(
        self,
        *,
        token: str,
        account_id: str,
        region: str,
        position_id: str,
    ) -> None:
        if not token or not account_id or not region:
            raise AssertionError("missing broker close routing data")
        self.closed.append(position_id)


async def run_day31_live_acceptance(base_mt5_service) -> None:
    factory = base_mt5_service._session_factory
    cipher = base_mt5_service._cipher
    with factory() as session:
        already = session.scalar(
            text(
                """
                SELECT count(*) FROM audit_events
                WHERE event_type='day31.live_acceptance_completed'
                  AND created_at > now() - interval '12 hours'
                """
            )
        )
        if already:
            logger.info("Day 31 live acceptance already completed recently; skipping")
            return
        owner_id = session.scalar(
            text(
                """
                SELECT u.id FROM users u
                JOIN user_roles ur ON ur.user_id=u.id
                JOIN roles r ON r.id=ur.role_id
                WHERE r.name='owner' AND u.status='active'
                ORDER BY u.created_at LIMIT 1
                """
            )
        )
        role_id = session.scalar(text("SELECT id FROM roles WHERE name='user' LIMIT 1"))
        signal_id = session.scalar(text("SELECT id FROM signals ORDER BY created_at DESC LIMIT 1"))
        if owner_id is None or role_id is None or signal_id is None:
            raise RuntimeError("Day 31 acceptance prerequisites are missing")
        marker = uuid4().hex[:10]
        user_id = session.scalar(
            text(
                """
                INSERT INTO users(email,display_name,status)
                VALUES(:email,'Day 31 Acceptance','active') RETURNING id
                """
            ),
            {"email": f"day31-{marker}@example.invalid"},
        )
        session.execute(
            text("INSERT INTO user_roles(user_id,role_id,granted_by_user_id) VALUES(:u,:r,:o)"),
            {"u": user_id, "r": role_id, "o": owner_id},
        )
        session.commit()

    fake_read = _FakeReadGateway([])
    fake_trade = _FakeTradeGateway()
    service = Day31TradingControlService(
        session_factory=factory,
        cipher=cipher,
        read_gateway=fake_read,  # type: ignore[arg-type]
        trade_gateway=fake_trade,  # type: ignore[arg-type]
    )

    default = service.get_settings(user_id)
    if default.risk_percent != Decimal("1.0") or not default.allow_double_lot:
        raise AssertionError("recommended Day 31 preset is not 1% + double-lot ON")
    if default.effective_double_lot_risk_percent != Decimal("2.0"):
        raise AssertionError("recommended double-lot effective risk is not 2%")

    blocked = service.activation_preview(user_id)
    if blocked.ready:
        raise AssertionError("activation passed before approved connected MT5 requirements")

    changed = service.update_risk(
        user_id=user_id,
        risk_percent=Decimal("1.5"),
        allow_double_lot=False,
    )
    if changed.risk_percent != Decimal("1.5") or changed.allow_double_lot:
        raise AssertionError("risk/toggle change did not persist")
    if changed.effective_double_lot_risk_percent != Decimal("1.5"):
        raise AssertionError("disabled double-lot should leave base risk unchanged")

    platform_token = base_mt5_service.resolve_platform_token()
    login = f"93{''.join(str(ord(ch) % 10) for ch in marker[:6])}"
    server = "VantageInternational-Live"
    metaapi_account_id = f"day31-{uuid4().hex}"
    token_ciphertext = cipher.encrypt(platform_token)
    token_fingerprint = cipher.fingerprint(platform_token)
    with factory() as session:
        session.execute(
            text(
                """
                INSERT INTO mt5_account_approvals(
                    user_id,broker,platform,account_environment,login,server,
                    approved_by_user_id,status
                ) VALUES(:u,'vantage','mt5','live',:login,:server,:owner,'active')
                """
            ),
            {"u": user_id, "login": login, "server": server, "owner": owner_id},
        )
        session.execute(
            text(
                """
                INSERT INTO mt5_accounts(
                    owner_user_id,broker,platform,account_environment,login,server,
                    metaapi_account_id,metaapi_token_ciphertext,metaapi_token_fingerprint,
                    status,remote_state,remote_connection_status,last_checked_at,last_connected_at
                ) VALUES(
                    :u,'vantage','mt5','live',:login,:server,:account_id,:ciphertext,:fingerprint,
                    'connected','DEPLOYED','CONNECTED',now(),now()
                )
                """
            ),
            {
                "u": user_id,
                "login": login,
                "server": server,
                "account_id": metaapi_account_id,
                "ciphertext": token_ciphertext,
                "fingerprint": token_fingerprint,
            },
        )
        session.commit()

    ready = service.activation_preview(user_id)
    if not ready.ready or "1.5% risk per position" not in ready.confirmation_message:
        raise AssertionError("activation preview did not show current effective risk")
    activated = service.activate(user_id=user_id, confirmed=True)
    if activated.trading_status != "active":
        raise AssertionError("activation did not persist")

    # Changing active risk must stop automation until the new effective risk is confirmed.
    reconfirm = service.update_risk(
        user_id=user_id,
        risk_percent=Decimal("1.0"),
        allow_double_lot=True,
    )
    if reconfirm.trading_status != "stopped" or reconfirm.effective_double_lot_risk_percent != Decimal("2.0"):
        raise AssertionError("active risk change did not stop/recalculate safely")
    service.activate(user_id=user_id, confirmed=True)

    bot1 = f"D31BOT{marker}A"
    bot2 = f"D31BOT{marker}B"
    manual = f"MANUAL{marker}"
    fake_read.positions = [
        {"id": bot1, "symbol": "XAUUSD"},
        {"id": bot2, "symbol": "XAUUSD"},
        {"id": manual, "symbol": "XAUUSD"},
    ]
    with factory() as session:
        for tp_index, broker_id in ((91, bot1), (92, bot2)):
            session.execute(
                text(
                    """
                    INSERT INTO positions(
                        signal_id,user_id,tp_index,planned_risk_percent,broker_position_id,
                        status,entry_price,stop_loss,take_profit,volume,opened_at
                    ) VALUES(:signal,:user,:tp,1.0,:broker,'open',4400,4390,4410,0.01,now())
                    """
                ),
                {
                    "signal": signal_id,
                    "user": user_id,
                    "tp": tp_index,
                    "broker": broker_id,
                },
            )
        session.commit()

    stopped = await service.stop_and_close(user_id)
    if stopped.settings.trading_status != "stopped":
        raise AssertionError("Stop and Close did not block new trades")
    if stopped.positions_closed != 2 or stopped.broker_actions_sent != 2:
        raise AssertionError("Stop and Close did not close exactly two mapped positions")
    if set(fake_trade.closed) != {bot1, bot2}:
        raise AssertionError("Stop and Close touched the wrong broker positions")
    if manual in fake_trade.closed:
        raise AssertionError("manual broker position was touched")

    before_replay = len(fake_trade.closed)
    replay = await service.stop_and_close(user_id)
    if len(fake_trade.closed) != before_replay or replay.broker_actions_sent != 0:
        raise AssertionError("Stop and Close replay duplicated broker actions")

    with factory() as session:
        rows = session.execute(
            text(
                """
                SELECT broker_position_id,status,close_reason FROM positions
                WHERE user_id=:u AND tp_index IN (91,92) ORDER BY tp_index
                """
            ),
            {"u": user_id},
        ).mappings().all()
        if any(row["status"] != "closed" or row["close_reason"] != "user_stop_close" for row in rows):
            raise AssertionError("mapped position close state was not persisted")
        session.add(
            AuditEvent(
                actor_user_id=owner_id,
                event_type="day31.live_acceptance_completed",
                entity_type="user_trading_controls",
                entity_id=user_id,
                payload={
                    "acceptance_version": "day31-live-2026-08-12",
                    "recommended_default_risk_percent": "1.0",
                    "recommended_double_lot_on": True,
                    "recommended_effective_double_risk_percent": "2.0",
                    "risk_change_reflected": True,
                    "toggle_change_reflected": True,
                    "activation_blocked_before_requirements": True,
                    "activation_effective_risk_confirmed": True,
                    "active_risk_change_forced_reconfirmation": True,
                    "stop_blocked_new_trades_first": True,
                    "mapped_bot_positions_closed": 2,
                    "manual_positions_touched": 0,
                    "stop_replay_broker_actions": 0,
                    "temporary_user_revoked": True,
                },
            )
        )
        session.execute(text("UPDATE users SET status='revoked',updated_at=now() WHERE id=:u"), {"u": user_id})
        session.execute(text("UPDATE mt5_account_approvals SET status='revoked',updated_at=now() WHERE user_id=:u"), {"u": user_id})
        session.execute(text("UPDATE mt5_accounts SET status='revoked',updated_at=now() WHERE owner_user_id=:u"), {"u": user_id})
        session.commit()

    logger.info(
        "Day 31 LIVE acceptance PASSED: default=1%% double=on effective_double=2%% activation_gate=true bot_closed=2 manual_touched=0 replay_actions=0"
    )
