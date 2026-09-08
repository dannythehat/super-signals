"""Provider Lab completeness runtime.

Day 15 closes the remaining research-enrollment gaps without weakening execution safety:
- every accepted SHADOW signal gets a durable enrollment-audit state;
- complete structured signals missed by an earlier bridge are repaired idempotently;
- a same-message bare NOW -> complete edit can create its first shadow row;
- genuinely fresh exact bare Gold/XAUUSD NOW calls can be enrolled from the first fresh
  public executable quote using the existing 50/100-pip research profile;
- stale/unbenchmarkable accepted signals are explicitly excluded instead of disappearing.

This module is research-only. It never calls a broker mutation API and never grants
live-money execution authority.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.bare_gold_now_policy import (
    STOP_LOSS_DISTANCE,
    TAKE_PROFIT_DISTANCE,
    bare_now_side,
)
from app.provider_fairness import (
    BENCHMARK_MODEL,
    BENCHMARK_RISK_PER_LEG_USD,
    BENCHMARK_START_BALANCE_USD,
    session_bucket,
)
from app.provider_profile_pit import (
    PIT_LEGACY_UNRESOLVABLE,
    PIT_RESOLVED,
    resolve_provider_profile_as_of,
)
from app.shadow_trading_service_v4 import ShadowTradeService as _ShadowTradeService
from app.shadow_trading_v4 import ShadowTradeManager as _BaseShadowTradeManager
from app.shadow_trading_v2 import _decimal

logger = logging.getLogger(__name__)

_AIDY_STYLES = {"intraday", "swing_or_sparse"}
_MAX_FRESH_BARE_AGE_SECONDS = 90
_MAX_SCORABLE_BARE_ENTRY_DELAY_MS = 20_000
_BATCH_LIMIT = 200


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class ShadowTradeManager(_BaseShadowTradeManager):
    """Keep Provider Lab accepted-signal enrollment complete and measurable."""

    async def poll_once(self) -> int:
        await asyncio.to_thread(self._sync_enrollment_audit_sync)
        repaired = await asyncio.to_thread(self._repair_structured_pending_sync)
        await asyncio.to_thread(self._enforce_scalper_exclusion_sync)

        bare_candidates = await asyncio.to_thread(self._fresh_bare_candidates_sync)
        rows = await asyncio.to_thread(self._active_rows)
        bare_signal_ids = await asyncio.to_thread(self._bare_enrolled_signal_ids_sync)
        public_rows = self._public_resolution_rows(rows, bare_signal_ids)

        if not bare_candidates and not public_rows:
            await asyncio.to_thread(self._expire_unresolved_pending_sync)
            return repaired

        bid, ask = await self._public_bid_ask()
        if bid is None or ask is None:
            await asyncio.to_thread(self._expire_unresolved_pending_sync)
            return repaired

        enrolled = 0
        if bare_candidates:
            enrolled = await asyncio.to_thread(
                self._enroll_bare_candidates_sync,
                bare_candidates,
                bid,
                ask,
            )

        await asyncio.to_thread(self._expire_unresolved_pending_sync)
        rows = await asyncio.to_thread(self._active_rows)
        bare_signal_ids = await asyncio.to_thread(self._bare_enrolled_signal_ids_sync)
        public_rows = self._public_resolution_rows(rows, bare_signal_ids)
        evaluated = 0
        if public_rows:
            evaluated = await self._evaluate_public_rows(
                public_rows,
                bid=bid,
                ask=ask,
                quote_mode="snapshot_poll",
            )
        return repaired + enrolled + evaluated

    def _sync_enrollment_audit_sync(self) -> None:
        """Make the audit ledger total over accepted shadow signals."""
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO provider_shadow_enrollment_audit(
                        signal_id,source_id,message_id,status,reason,provider_signal_posted_at,
                        enrollment_observed_at,quote_mode,entry_delay_ms
                    )
                    SELECT s.id,s.source_id,s.source_message_id,
                           CASE WHEN st.signal_id IS NULL THEN 'pending' ELSE 'enrolled' END,
                           CASE WHEN st.signal_id IS NULL THEN 'runtime_review_pending'
                                ELSE 'existing_shadow_trade' END,
                           s.source_posted_at,
                           CASE WHEN st.signal_id IS NULL THEN NULL ELSE st.created_at END,
                           CASE WHEN st.signal_id IS NULL THEN NULL ELSE st.quote_mode END,
                           CASE WHEN st.signal_id IS NULL THEN NULL ELSE st.entry_delay_ms END
                    FROM signals s
                    JOIN sources src ON src.id=s.source_id
                    LEFT JOIN LATERAL (
                        SELECT signal_id,created_at,quote_mode,entry_delay_ms
                        FROM shadow_trades
                        WHERE signal_id=s.id
                        ORDER BY created_at ASC
                        LIMIT 1
                    ) st ON true
                    LEFT JOIN provider_shadow_enrollment_audit a ON a.signal_id=s.id
                    WHERE src.status='shadow' AND s.parser_status='accepted' AND a.signal_id IS NULL
                    ON CONFLICT (signal_id) DO NOTHING
                    """
                )
            )
            session.execute(
                text(
                    """
                    UPDATE provider_shadow_enrollment_audit a
                    SET status='enrolled',
                        reason=CASE WHEN a.reason LIKE 'bare_profile_%' THEN a.reason
                                    ELSE 'shadow_trade_present' END,
                        enrollment_observed_at=COALESCE(a.enrollment_observed_at,st.created_at),
                        quote_mode=COALESCE(a.quote_mode,st.quote_mode),
                        entry_delay_ms=COALESCE(a.entry_delay_ms,st.entry_delay_ms),
                        updated_at=now()
                    FROM LATERAL (
                        SELECT created_at,quote_mode,entry_delay_ms
                        FROM shadow_trades
                        WHERE signal_id=a.signal_id
                        ORDER BY created_at ASC
                        LIMIT 1
                    ) st
                    WHERE a.status<>'enrolled'
                    """
                )
            )
            session.commit()

    def _repair_structured_pending_sync(self) -> int:
        with self._session_factory() as session:
            rows = list(
                session.execute(
                    text(
                        """
                        SELECT a.signal_id,s.source_revision_index
                        FROM provider_shadow_enrollment_audit a
                        JOIN signals s ON s.id=a.signal_id
                        WHERE a.status='pending'
                          AND s.parser_status='accepted'
                          AND s.entry_low IS NOT NULL AND s.entry_high IS NOT NULL
                          AND s.stop_loss IS NOT NULL
                          AND jsonb_array_length(s.take_profits)>0
                        ORDER BY s.source_posted_at,a.signal_id
                        LIMIT :limit
                        """
                    ),
                    {"limit": _BATCH_LIMIT},
                ).mappings()
            )
        if not rows:
            return 0

        service = _ShadowTradeService(self._session_factory)
        repaired = 0
        for row in rows:
            signal_id = UUID(str(row["signal_id"]))
            try:
                if int(row["source_revision_index"] or 0) == 0:
                    service.record_signal(signal_id)
                else:
                    self._insert_revised_structured_signal_sync(signal_id)
            except Exception:
                logger.exception(
                    "Provider Lab structured enrollment repair failed safely signal=%s",
                    signal_id,
                )
                continue
            if self._shadow_exists_sync(signal_id):
                self._mark_audit_enrolled_sync(signal_id, reason="structured_repair_enrolled")
                repaired += 1
        return repaired

    def _insert_revised_structured_signal_sync(self, signal_id: UUID) -> bool:
        """Create first shadow state from a now-complete same-message revision.

        Existing v4 deliberately refuses to mutate an already-created AIDY shadow row on
        revisions. That rule remains. This path is only for the opposite case: there is
        no shadow row at all because the original message was incomplete/bare.
        """
        if self._shadow_exists_sync(signal_id):
            return False

        with self._session_factory() as session:
            row = session.execute(
                text(
                    """
                    SELECT s.id AS signal_id,s.source_revision_index,m.id AS message_id,
                           m.source_id,s.source_posted_at AS posted_at,src.status AS source_status,
                           s.symbol,s.side,s.order_type,s.entry_low,s.entry_high,s.stop_loss,
                           s.take_profits,s.has_open_runner,s.original_text
                    FROM signals s
                    JOIN messages m ON m.id=s.source_message_id
                    JOIN sources src ON src.id=m.source_id
                    WHERE s.id=:signal_id AND s.parser_status='accepted'
                      AND src.status='shadow' AND m.deleted_at IS NULL
                    LIMIT 1
                    """
                ),
                {"signal_id": signal_id},
            ).mappings().first()
            if row is None or int(row["source_revision_index"] or 0) <= 0:
                return False

            posted_at = row["posted_at"]
            if not isinstance(posted_at, datetime):
                return False
            posted_at = _utc(posted_at)
            profile = resolve_provider_profile_as_of(
                session,
                source_id=UUID(str(row["source_id"])),
                as_of=posted_at,
            )
            pit_status = PIT_RESOLVED if profile is not None else PIT_LEGACY_UNRESOLVABLE
            provider_style = profile.style if profile is not None else "unknown"
            readiness = profile.interpretation_readiness if profile is not None else 0.0

            stop = _decimal(row["stop_loss"])
            targets = tuple(
                value
                for value in (_decimal(item) for item in (row["take_profits"] or []))
                if value is not None
            )
            runner = bool(row["has_open_runner"])
            entries = _ShadowTradeService._research_entries(row)
            if (
                stop is None
                or not entries
                or (not targets and not runner)
                or row["side"] not in {"BUY", "SELL"}
            ):
                return False

            initial_exclusion = (
                "legacy_profile_unresolvable"
                if pit_status == PIT_LEGACY_UNRESOLVABLE
                else "unsupported_style_scalper"
                if provider_style == "scalper"
                else "market_data_not_observed"
            )
            created = False
            for entry in entries:
                legs = [
                    {"tp_index": index, "target_price": str(target), "is_runner": False}
                    for index, target in enumerate(targets, start=1)
                ]
                if runner:
                    legs.append(
                        {"tp_index": len(targets) + 1, "target_price": None, "is_runner": True}
                    )
                geometry = {
                    "side": str(row["side"]),
                    "entry_order_type": entry.order_type,
                    "entry_low": str(entry.low),
                    "entry_high": str(entry.high),
                    "initial_stop": str(stop),
                    "entry_index": entry.entry_index,
                    "legs": legs,
                }
                shadow_id = session.execute(
                    text(
                        """
                        INSERT INTO shadow_trades(
                            source_id,signal_id,message_id,symbol,side,order_type,entry_low,
                            entry_high,initial_stop,current_stop,take_profits,status,
                            benchmark_model,benchmark_start_balance_usd,benchmark_risk_per_leg_usd,
                            entry_index,entry_order_type,provider_style,interpretation_readiness_at_entry,
                            signal_posted_at,session_bucket,weekday_iso,target_count,score_eligible,
                            score_exclusion_reason,quote_mode,aidy_original_geometry,aidy_effective_stop,
                            provider_profile_version_id,provider_profile_version_no,
                            provider_profile_effective_at,provider_profile_pit_status
                        ) VALUES (
                            :source_id,:signal_id,:message_id,:symbol,:side,:order_type,:entry_low,
                            :entry_high,:stop,:stop,CAST(:take_profits AS jsonb),'pending',
                            :benchmark_model,:benchmark_balance,:benchmark_risk,:entry_index,:entry_order_type,
                            :provider_style,:readiness,:posted_at,:session_bucket,:weekday_iso,:target_count,
                            false,:exclusion,'unobserved',CAST(:geometry AS jsonb),:stop,
                            :profile_id,:profile_no,:profile_effective_at,:pit_status
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
                        "order_type": "market" if entry.order_type == "market" else "pending",
                        "entry_low": entry.low,
                        "entry_high": entry.high,
                        "stop": stop,
                        "take_profits": json.dumps([str(value) for value in targets]),
                        "benchmark_model": BENCHMARK_MODEL,
                        "benchmark_balance": BENCHMARK_START_BALANCE_USD,
                        "benchmark_risk": BENCHMARK_RISK_PER_LEG_USD,
                        "entry_index": entry.entry_index,
                        "entry_order_type": entry.order_type,
                        "provider_style": provider_style,
                        "readiness": readiness,
                        "posted_at": posted_at,
                        "session_bucket": session_bucket(posted_at),
                        "weekday_iso": posted_at.isoweekday(),
                        "target_count": len(targets) + int(runner),
                        "exclusion": initial_exclusion,
                        "geometry": json.dumps(geometry, separators=(",", ":")),
                        "profile_id": profile.id if profile is not None else None,
                        "profile_no": profile.version_no if profile is not None else None,
                        "profile_effective_at": profile.effective_at if profile is not None else None,
                        "pit_status": pit_status,
                    },
                ).scalar_one_or_none()
                if shadow_id is None:
                    continue
                created = True
                for index, target in enumerate(targets, start=1):
                    session.execute(
                        text(
                            """
                            INSERT INTO shadow_trade_legs(
                                shadow_trade_id,source_id,signal_id,tp_index,target_price,is_runner,
                                aidy_original_target,aidy_effective_target
                            ) VALUES (:shadow_id,:source_id,:signal_id,:tp_index,:target,false,:target,:target)
                            ON CONFLICT (shadow_trade_id,tp_index) DO NOTHING
                            """
                        ),
                        {
                            "shadow_id": shadow_id,
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
                                shadow_trade_id,source_id,signal_id,tp_index,target_price,is_runner,
                                aidy_original_target,aidy_effective_target
                            ) VALUES (:shadow_id,:source_id,:signal_id,:tp_index,NULL,true,NULL,NULL)
                            ON CONFLICT (shadow_trade_id,tp_index) DO NOTHING
                            """
                        ),
                        {
                            "shadow_id": shadow_id,
                            "source_id": row["source_id"],
                            "signal_id": signal_id,
                            "tp_index": len(targets) + 1,
                        },
                    )
            session.commit()
            return created

    def _fresh_bare_candidates_sync(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        candidates: list[dict[str, Any]] = []
        with self._session_factory() as session:
            rows = list(
                session.execute(
                    text(
                        """
                        SELECT a.signal_id,a.created_at,s.source_id,s.source_message_id AS message_id,
                               s.source_posted_at,s.symbol,s.side,s.original_text
                        FROM provider_shadow_enrollment_audit a
                        JOIN signals s ON s.id=a.signal_id
                        WHERE a.status='pending'
                          AND s.parser_status='accepted'
                          AND s.entry_low IS NULL AND s.entry_high IS NULL
                          AND s.stop_loss IS NULL AND jsonb_array_length(s.take_profits)=0
                        ORDER BY s.source_posted_at,a.signal_id
                        LIMIT :limit
                        """
                    ),
                    {"limit": _BATCH_LIMIT},
                ).mappings()
            )
        for row in rows:
            posted_at = row["source_posted_at"]
            if not isinstance(posted_at, datetime):
                continue
            age = (now - _utc(posted_at)).total_seconds()
            side = bare_now_side(str(row["original_text"] or ""))
            if side is None:
                self._mark_audit_excluded_sync(
                    UUID(str(row["signal_id"])),
                    "accepted_without_benchmarkable_geometry",
                )
                continue
            if age > _MAX_FRESH_BARE_AGE_SECONDS:
                self._mark_audit_excluded_sync(
                    UUID(str(row["signal_id"])),
                    "bare_profile_no_fresh_quote_within_90s",
                )
                continue
            item = dict(row)
            item["bare_side"] = side
            candidates.append(item)
        return candidates

    def _enroll_bare_candidates_sync(
        self,
        rows: list[dict[str, Any]],
        bid: Decimal,
        ask: Decimal,
    ) -> int:
        observed_at = datetime.now(UTC)
        created = 0
        for row in rows:
            signal_id = UUID(str(row["signal_id"]))
            if self._shadow_exists_sync(signal_id):
                self._mark_audit_enrolled_sync(signal_id, reason="bare_profile_shadow_present")
                continue
            posted_at = row["source_posted_at"]
            if not isinstance(posted_at, datetime):
                self._mark_audit_excluded_sync(signal_id, "provider_signal_timestamp_invalid")
                continue
            provider_posted_at = _utc(posted_at)
            delay_ms = max(0, int((observed_at - provider_posted_at).total_seconds() * 1000))
            if delay_ms > _MAX_FRESH_BARE_AGE_SECONDS * 1000:
                self._mark_audit_excluded_sync(
                    signal_id,
                    "bare_profile_no_fresh_quote_within_90s",
                )
                continue

            side = str(row["bare_side"])
            entry = ask if side == "BUY" else bid
            stop = entry - STOP_LOSS_DISTANCE if side == "BUY" else entry + STOP_LOSS_DISTANCE
            target = entry + TAKE_PROFIT_DISTANCE if side == "BUY" else entry - TAKE_PROFIT_DISTANCE
            if entry <= 0 or stop <= 0 or target <= 0:
                self._mark_audit_excluded_sync(signal_id, "bare_profile_quote_geometry_invalid")
                continue

            with self._session_factory() as session:
                profile = resolve_provider_profile_as_of(
                    session,
                    source_id=UUID(str(row["source_id"])),
                    as_of=provider_posted_at,
                )
                pit_status = PIT_RESOLVED if profile is not None else PIT_LEGACY_UNRESOLVABLE
                provider_style = profile.style if profile is not None else "unknown"
                readiness = profile.interpretation_readiness if profile is not None else 0.0
                exclusion = (
                    "legacy_profile_unresolvable"
                    if profile is None
                    else "unsupported_style_scalper"
                    if provider_style == "scalper"
                    else "bare_profile_entry_delay_over_scoring_gate"
                    if delay_ms > _MAX_SCORABLE_BARE_ENTRY_DELAY_MS
                    else "market_data_not_observed"
                )
                geometry = {
                    "side": side,
                    "entry_order_type": "market",
                    "entry_low": str(entry),
                    "entry_high": str(entry),
                    "initial_stop": str(stop),
                    "entry_index": 1,
                    "legs": [
                        {"tp_index": 1, "target_price": str(target), "is_runner": False}
                    ],
                    "entry_source": "first_fresh_public_executable_quote",
                    "provider_signal_posted_at": provider_posted_at.isoformat(),
                    "enrollment_observed_at": observed_at.isoformat(),
                }
                shadow_id = session.execute(
                    text(
                        """
                        INSERT INTO shadow_trades(
                            source_id,signal_id,message_id,symbol,side,order_type,entry_low,
                            entry_high,initial_stop,current_stop,take_profits,status,
                            benchmark_model,benchmark_start_balance_usd,benchmark_risk_per_leg_usd,
                            entry_index,entry_order_type,provider_style,interpretation_readiness_at_entry,
                            signal_posted_at,session_bucket,weekday_iso,target_count,score_eligible,
                            score_exclusion_reason,quote_mode,aidy_original_geometry,aidy_effective_stop,
                            provider_profile_version_id,provider_profile_version_no,
                            provider_profile_effective_at,provider_profile_pit_status,
                            entry_delay_ms
                        ) VALUES (
                            :source_id,:signal_id,:message_id,'XAUUSD',:side,'market',:entry,
                            :entry,:stop,:stop,CAST(:take_profits AS jsonb),'pending',
                            :benchmark_model,:benchmark_balance,:benchmark_risk,1,'market',
                            :provider_style,:readiness,:enrollment_time,:session_bucket,:weekday_iso,1,
                            false,:exclusion,'unobserved',CAST(:geometry AS jsonb),:stop,
                            :profile_id,:profile_no,:profile_effective_at,:pit_status,:entry_delay_ms
                        )
                        ON CONFLICT (signal_id,entry_index) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "source_id": row["source_id"],
                        "signal_id": signal_id,
                        "message_id": row["message_id"],
                        "side": side,
                        "entry": entry,
                        "stop": stop,
                        "take_profits": json.dumps([str(target)]),
                        "benchmark_model": BENCHMARK_MODEL,
                        "benchmark_balance": BENCHMARK_START_BALANCE_USD,
                        "benchmark_risk": BENCHMARK_RISK_PER_LEG_USD,
                        "provider_style": provider_style,
                        "readiness": readiness,
                        # Replay starts when the executable quote was actually observed;
                        # the provider's original post time remains immutable in the audit.
                        "enrollment_time": observed_at,
                        "session_bucket": session_bucket(provider_posted_at),
                        "weekday_iso": provider_posted_at.isoweekday(),
                        "exclusion": exclusion,
                        "geometry": json.dumps(geometry, separators=(",", ":")),
                        "profile_id": profile.id if profile is not None else None,
                        "profile_no": profile.version_no if profile is not None else None,
                        "profile_effective_at": profile.effective_at if profile is not None else None,
                        "pit_status": pit_status,
                        "entry_delay_ms": delay_ms,
                    },
                ).scalar_one_or_none()
                if shadow_id is None:
                    session.rollback()
                    continue
                session.execute(
                    text(
                        """
                        INSERT INTO shadow_trade_legs(
                            shadow_trade_id,source_id,signal_id,tp_index,target_price,is_runner,
                            aidy_original_target,aidy_effective_target
                        ) VALUES (:shadow_id,:source_id,:signal_id,1,:target,false,:target,:target)
                        ON CONFLICT (shadow_trade_id,tp_index) DO NOTHING
                        """
                    ),
                    {
                        "shadow_id": shadow_id,
                        "source_id": row["source_id"],
                        "signal_id": signal_id,
                        "target": target,
                    },
                )
                session.commit()
            audit_reason = (
                "bare_profile_fresh_quote"
                if delay_ms <= _MAX_SCORABLE_BARE_ENTRY_DELAY_MS
                else "bare_profile_late_quote_research_only"
            )
            self._mark_audit_enrolled_sync(
                signal_id,
                reason=audit_reason,
                observed_at=observed_at,
                quote_mode="snapshot_poll",
                entry_delay_ms=delay_ms,
            )
            created += 1
        return created

    def _expire_unresolved_pending_sync(self) -> int:
        """No accepted signal may remain silently pending forever."""
        with self._session_factory() as session:
            result = session.execute(
                text(
                    """
                    UPDATE provider_shadow_enrollment_audit a
                    SET status='excluded',
                        reason='enrollment_evidence_unavailable_after_90s',
                        updated_at=now()
                    FROM signals s
                    WHERE a.signal_id=s.id
                      AND a.status='pending'
                      AND s.source_posted_at < now() - interval '90 seconds'
                    """
                )
            )
            session.commit()
            return int(result.rowcount or 0)

    def _bare_enrolled_signal_ids_sync(self) -> set[UUID]:
        with self._session_factory() as session:
            values = session.execute(
                text(
                    """
                    SELECT signal_id FROM provider_shadow_enrollment_audit
                    WHERE status='enrolled' AND reason LIKE 'bare_profile_%'
                    """
                )
            ).scalars().all()
        return {UUID(str(value)) for value in values}

    @staticmethod
    def _public_resolution_rows(
        rows: list[Any],
        bare_signal_ids: set[UUID],
    ) -> list[Any]:
        selected: list[Any] = []
        for row in rows:
            style = str(row["provider_style"])
            signal_id = UUID(str(row["signal_id"]))
            if style not in _AIDY_STYLES | {"scalper"}:
                selected.append(row)
                continue
            if signal_id not in bare_signal_ids:
                continue
            exclusion = str(row["score_exclusion_reason"] or "")
            if style == "scalper" or exclusion != "market_data_not_observed":
                # Outcome observation is useful, but these rows remain formally unscored.
                selected.append(row)
        return selected

    def _shadow_exists_sync(self, signal_id: UUID) -> bool:
        with self._session_factory() as session:
            return bool(
                session.execute(
                    text("SELECT EXISTS(SELECT 1 FROM shadow_trades WHERE signal_id=:id)"),
                    {"id": signal_id},
                ).scalar_one()
            )

    def _mark_audit_enrolled_sync(
        self,
        signal_id: UUID,
        *,
        reason: str,
        observed_at: datetime | None = None,
        quote_mode: str | None = None,
        entry_delay_ms: int | None = None,
    ) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE provider_shadow_enrollment_audit
                    SET status='enrolled',reason=:reason,
                        enrollment_observed_at=COALESCE(:observed_at,enrollment_observed_at,now()),
                        quote_mode=COALESCE(:quote_mode,quote_mode),
                        entry_delay_ms=COALESCE(:entry_delay_ms,entry_delay_ms),updated_at=now()
                    WHERE signal_id=:signal_id
                    """
                ),
                {
                    "signal_id": signal_id,
                    "reason": reason,
                    "observed_at": observed_at,
                    "quote_mode": quote_mode,
                    "entry_delay_ms": entry_delay_ms,
                },
            )
            session.commit()

    def _mark_audit_excluded_sync(self, signal_id: UUID, reason: str) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    UPDATE provider_shadow_enrollment_audit
                    SET status='excluded',reason=:reason,updated_at=now()
                    WHERE signal_id=:signal_id AND status='pending'
                    """
                ),
                {"signal_id": signal_id, "reason": reason[:120]},
            )
            session.commit()


__all__ = ["ShadowTradeManager"]
