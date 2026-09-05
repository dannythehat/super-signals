"""Revision-safe signal enrollment for the fair Provider Lab benchmark."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text

from app.provider_fairness import (
    BENCHMARK_MODEL,
    BENCHMARK_RISK_PER_LEG_USD,
    BENCHMARK_START_BALANCE_USD,
    session_bucket,
)
from app.shadow_trading_v2 import ShadowTradeService as _BaseShadowTradeService, _decimal


class ShadowTradeService(_BaseShadowTradeService):
    """Create only real-time benchmark actions and quarantine conflicting revisions."""

    def record_signal(self, signal_id: UUID) -> bool:
        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT s.id AS signal_id,s.source_revision_index,m.id AS message_id,
                           m.source_id,s.source_posted_at AS posted_at,src.status AS source_status,
                           s.symbol,s.side,s.order_type,s.entry_low,s.entry_high,s.stop_loss,
                           s.take_profits,s.has_open_runner,s.original_text,
                           COALESCE(pr.style,'unknown') AS provider_style,
                           COALESCE(pr.interpretation_readiness,0) AS interpretation_readiness
                    FROM signals s
                    JOIN messages m ON m.id=s.source_message_id
                    JOIN sources src ON src.id=m.source_id
                    LEFT JOIN provider_research_profiles pr ON pr.source_id=m.source_id
                    WHERE s.id=:signal_id AND s.parser_status='accepted'
                      AND src.status IN ('shadow','testing','live') AND m.deleted_at IS NULL
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if row is None:
                return False

            revision_index = int(row["source_revision_index"] or 0)
            source_status = str(row["source_status"] or "")
            if revision_index > 0:
                if source_status == "shadow":
                    # Shadow source revisions can otherwise rewrite historical benchmark
                    # geometry. Quarantine any already-enrolled active action.
                    updated = session.execute(
                        text(
                            """
                            UPDATE shadow_trades
                            SET score_eligible=false,
                                score_exclusion_reason='material_same_message_revision_requires_review',
                                updated_at=now()
                            WHERE signal_id=:signal_id AND status IN ('pending','open')
                            RETURNING id
                            """
                        ),
                        {"signal_id": signal_id},
                    ).all()
                    if updated:
                        session.commit()
                    return False

                # testing/live may first become executable on a provider edit. If no
                # benchmark row exists yet, enrolling that real-time canonical revision
                # is prospective evidence, not hindsight. Never create a second action
                # for a signal already mirrored earlier.
                existing = session.execute(
                    text("SELECT 1 FROM shadow_trades WHERE signal_id=:signal_id LIMIT 1"),
                    {"signal_id": signal_id},
                ).scalar_one_or_none()
                if existing is not None:
                    return False

            stop = _decimal(row["stop_loss"])
            targets = tuple(
                value
                for value in (_decimal(item) for item in (row["take_profits"] or []))
                if value is not None
            )
            runner = bool(row["has_open_runner"])
            entries = self._research_entries(row)
            if stop is None or not entries or (not targets and not runner) or row["side"] not in {"BUY", "SELL"}:
                return False

            posted_at = row["posted_at"]
            if not isinstance(posted_at, datetime):
                return False
            posted_at = posted_at.replace(tzinfo=UTC) if posted_at.tzinfo is None else posted_at.astimezone(UTC)

            created_any = False
            for entry in entries:
                shadow_id = session.execute(
                    text(
                        """
                        INSERT INTO shadow_trades(
                            source_id,signal_id,message_id,symbol,side,order_type,entry_low,
                            entry_high,initial_stop,current_stop,take_profits,status,
                            benchmark_model,benchmark_start_balance_usd,benchmark_risk_per_leg_usd,
                            entry_index,entry_order_type,provider_style,interpretation_readiness_at_entry,
                            signal_posted_at,session_bucket,weekday_iso,target_count,score_eligible,
                            score_exclusion_reason,quote_mode
                        ) VALUES (
                            :source_id,:signal_id,:message_id,:symbol,:side,:broad_order_type,:entry_low,
                            :entry_high,:stop,:stop,CAST(:take_profits AS jsonb),'pending',
                            :benchmark_model,:benchmark_balance,:benchmark_risk,:entry_index,:entry_order_type,
                            :provider_style,:readiness,:posted_at,:session_bucket,:weekday_iso,:target_count,
                            false,'market_data_not_observed','unobserved'
                        )
                        ON CONFLICT (signal_id,entry_index) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "source_id": row["source_id"],
                        "signal_id": signal_id,
                        "message_id": row["message_id"],
                        "symbol": str(row["symbol"] or "XAUUSD").upper(),
                        "side": row["side"],
                        "broad_order_type": "market" if entry.order_type == "market" else "pending",
                        "entry_low": entry.low,
                        "entry_high": entry.high,
                        "stop": stop,
                        "take_profits": json.dumps([str(value) for value in targets]),
                        "benchmark_model": BENCHMARK_MODEL,
                        "benchmark_balance": BENCHMARK_START_BALANCE_USD,
                        "benchmark_risk": BENCHMARK_RISK_PER_LEG_USD,
                        "entry_index": entry.entry_index,
                        "entry_order_type": entry.order_type,
                        "provider_style": str(row["provider_style"] or "unknown"),
                        "readiness": row["interpretation_readiness"],
                        "posted_at": posted_at,
                        "session_bucket": session_bucket(posted_at),
                        "weekday_iso": posted_at.isoweekday(),
                        "target_count": len(targets) + int(runner),
                    },
                ).scalar_one_or_none()
                if shadow_id is None:
                    continue
                created_any = True
                for index, target in enumerate(targets, start=1):
                    session.execute(
                        text(
                            """
                            INSERT INTO shadow_trade_legs(
                                shadow_trade_id,source_id,signal_id,tp_index,target_price,is_runner
                            ) VALUES (:shadow_trade_id,:source_id,:signal_id,:tp_index,:target,false)
                            ON CONFLICT (shadow_trade_id,tp_index) DO NOTHING
                            """
                        ),
                        {
                            "shadow_trade_id": shadow_id,
                            "source_id": row["source_id"],
                            "signal_id": signal_id,
                            "tp_index": index,
                            "target": target,
                        },
                    )
                if runner:
                    session.execute(
                        text(
                            """
                            INSERT INTO shadow_trade_legs(
                                shadow_trade_id,source_id,signal_id,tp_index,target_price,is_runner
                            ) VALUES (:shadow_trade_id,:source_id,:signal_id,:tp_index,NULL,true)
                            ON CONFLICT (shadow_trade_id,tp_index) DO NOTHING
                            """
                        ),
                        {
                            "shadow_trade_id": shadow_id,
                            "source_id": row["source_id"],
                            "signal_id": signal_id,
                            "tp_index": len(targets) + 1,
                        },
                    )
            session.commit()
            return created_any


__all__ = ["ShadowTradeService"]
