"""PostgreSQL-safe completeness wrapper for Provider Lab enrollment."""

from __future__ import annotations

from sqlalchemy import text

from app.shadow_trading_v5 import ShadowTradeManager as _BaseShadowTradeManager


class ShadowTradeManager(_BaseShadowTradeManager):
    """Day 15 runtime with a PostgreSQL-safe total-audit synchronizer."""

    def _sync_enrollment_audit_sync(self) -> None:
        with self._session_factory() as session:
            session.execute(
                text(
                    """
                    INSERT INTO provider_shadow_enrollment_audit(
                        signal_id,source_id,message_id,status,reason,provider_signal_posted_at,
                        enrollment_observed_at,quote_mode,entry_delay_ms
                    )
                    SELECT s.id,s.source_id,s.source_message_id,
                           CASE WHEN EXISTS(
                               SELECT 1 FROM shadow_trades st WHERE st.signal_id=s.id
                           ) THEN 'enrolled' ELSE 'pending' END,
                           CASE WHEN EXISTS(
                               SELECT 1 FROM shadow_trades st WHERE st.signal_id=s.id
                           ) THEN 'existing_shadow_trade' ELSE 'runtime_review_pending' END,
                           s.source_posted_at,
                           (SELECT st.created_at FROM shadow_trades st
                            WHERE st.signal_id=s.id ORDER BY st.created_at ASC LIMIT 1),
                           (SELECT st.quote_mode FROM shadow_trades st
                            WHERE st.signal_id=s.id ORDER BY st.created_at ASC LIMIT 1),
                           (SELECT st.entry_delay_ms FROM shadow_trades st
                            WHERE st.signal_id=s.id ORDER BY st.created_at ASC LIMIT 1)
                    FROM signals s
                    JOIN sources src ON src.id=s.source_id
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
                        enrollment_observed_at=COALESCE(
                            a.enrollment_observed_at,
                            (SELECT st.created_at FROM shadow_trades st
                             WHERE st.signal_id=a.signal_id ORDER BY st.created_at ASC LIMIT 1)
                        ),
                        quote_mode=COALESCE(
                            a.quote_mode,
                            (SELECT st.quote_mode FROM shadow_trades st
                             WHERE st.signal_id=a.signal_id ORDER BY st.created_at ASC LIMIT 1)
                        ),
                        entry_delay_ms=COALESCE(
                            a.entry_delay_ms,
                            (SELECT st.entry_delay_ms FROM shadow_trades st
                             WHERE st.signal_id=a.signal_id ORDER BY st.created_at ASC LIMIT 1)
                        ),
                        updated_at=now()
                    WHERE a.status<>'enrolled'
                      AND EXISTS(
                          SELECT 1 FROM shadow_trades st WHERE st.signal_id=a.signal_id
                      )
                    """
                )
            )
            session.commit()


__all__ = ["ShadowTradeManager"]
