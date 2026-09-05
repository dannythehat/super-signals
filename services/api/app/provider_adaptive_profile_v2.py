"""Signal-level performance refinement for adaptive provider intelligence.

Broker outcome tables are TP-leg based. Provider behavioural learning must not count a
three-TP signal as three independent trades, so this service collapses completed broker
legs back to one provider signal before time-to-win/time-to-loss and side/session analysis.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.provider_adaptive_profile import AdaptiveProviderProfileService


class SignalLevelAdaptiveProviderProfileService(AdaptiveProviderProfileService):
    """Use one completed provider signal as the unit of broker performance evidence."""

    @staticmethod
    def _broker_outcomes(session: Session, source_id: UUID) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT
                    signal_id,
                    MAX(side) AS side,
                    CASE
                        WHEN SUM(COALESCE(cash_pnl,0)) > 0 THEN 'won'
                        WHEN SUM(COALESCE(cash_pnl,0)) < 0 THEN 'lost'
                        ELSE 'breakeven'
                    END AS status,
                    MIN(opened_at) AS opened_at,
                    MAX(closed_at) AS closed_at,
                    SUM(COALESCE(cash_pnl,0)) AS cash_pnl,
                    SUM(COALESCE(return_percent,0)) AS return_percent,
                    CASE
                        WHEN COUNT(DISTINCT close_reason)=1 THEN MAX(close_reason)
                        ELSE 'mixed_signal_outcome'
                    END AS close_reason
                FROM performance_trade_outcomes
                WHERE source_id=:source_id
                GROUP BY signal_id
                HAVING BOOL_AND(
                    closed_at IS NOT NULL
                    AND status IN ('won','lost','breakeven','closed_unknown')
                )
                ORDER BY MAX(closed_at) DESC
                LIMIT :limit
                """
            ),
            {"source_id": source_id, "limit": 240},
        ).mappings().all()
        return [dict(row) for row in rows]


__all__ = ["SignalLevelAdaptiveProviderProfileService"]
