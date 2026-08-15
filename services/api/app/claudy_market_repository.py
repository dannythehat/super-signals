"""PostgreSQL persistence for Claudy Phase 0-lite market evidence.

This module stores recorder evidence only. It has no signal, position, lifecycle,
Telegram, risk-sizing, or broker-mutation write path.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


def _persisted_position_state_json(snapshot: dict[str, object]) -> object | None:
    """Persist position state only when the broker read explicitly succeeded.

    ``[]`` means the broker was queried successfully and returned no positions. SQL
    NULL means position state was not known at capture time, including read failure or
    any snapshot created before the positions endpoint was reached.
    """
    raw_availability = snapshot.get("data_availability_json")
    if not isinstance(raw_availability, str):
        return None
    try:
        availability = json.loads(raw_availability)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(availability, dict) or availability.get("positions") != "available":
        return None
    return snapshot.get("position_state_json")


class ClaudyMarketRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load_reference_demo_account(self, owner_user_id: UUID) -> Any | None:
        with self._session_factory() as session:
            return session.execute(
                text(
                    """
                    SELECT
                        id,
                        owner_user_id,
                        account_environment,
                        metaapi_account_id,
                        metaapi_token_ciphertext,
                        status
                    FROM mt5_accounts
                    WHERE owner_user_id=:owner_user_id
                      AND account_environment='demo'
                      AND status!='revoked'
                    LIMIT 1
                    """
                ),
                {"owner_user_id": owner_user_id},
            ).mappings().first()

    def provider_state_summary(self) -> dict[str, int]:
        """Store provider operating-state counts, never recent directions/signals."""
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT status, COUNT(*)::int AS count
                    FROM sources
                    GROUP BY status
                    ORDER BY status
                    """
                )
            ).mappings().all()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def store_candle(self, candle: dict[str, object]) -> tuple[UUID, int, bool]:
        """Append a candle revision only when its broker payload actually changes."""
        params = dict(candle)
        with self._session_factory() as session:
            existing = session.execute(
                text(
                    """
                    SELECT id, revision_index
                    FROM market_candles
                    WHERE source=:source
                      AND symbol=:symbol
                      AND timeframe=:timeframe
                      AND open_time_utc=:open_time_utc
                      AND payload_digest=:payload_digest
                    LIMIT 1
                    """
                ),
                params,
            ).mappings().first()
            if existing is not None:
                return existing["id"], int(existing["revision_index"]), False

            revision_index = int(
                session.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(revision_index),0) + 1
                        FROM market_candles
                        WHERE source=:source
                          AND symbol=:symbol
                          AND timeframe=:timeframe
                          AND open_time_utc=:open_time_utc
                        """
                    ),
                    params,
                ).scalar_one()
            )
            candle_id = session.execute(
                text(
                    """
                    INSERT INTO market_candles (
                        symbol,timeframe,open_time_utc,broker_open_time,
                        open,high,low,close,tick_volume,spread,volume,
                        source,revision_index,payload_digest,first_observed_at
                    )
                    VALUES (
                        :symbol,:timeframe,:open_time_utc,:broker_open_time,
                        :open,:high,:low,:close,:tick_volume,:spread,:volume,
                        :source,:revision_index,:payload_digest,:first_observed_at
                    )
                    RETURNING id
                    """
                ),
                {**params, "revision_index": revision_index},
            ).scalar_one()
            session.commit()
            return candle_id, revision_index, True

    def latest_candle_ids(self, *, symbol: str) -> dict[str, UUID]:
        """Return the latest broker revision of the latest closed candle per timeframe."""
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT DISTINCT ON (timeframe) timeframe, id
                    FROM market_candles
                    WHERE source='metaapi' AND symbol=:symbol
                    ORDER BY timeframe, open_time_utc DESC, revision_index DESC
                    """
                ),
                {"symbol": symbol},
            ).mappings().all()
        return {str(row["timeframe"]): row["id"] for row in rows}

    def event_observation_ids_known_at(
        self,
        *,
        captured_at: datetime,
        lookback_hours: int = 24,
    ) -> list[UUID]:
        """Return latest revisions that had actually been observed by capture time.

        The first-observed timestamp is the point-in-time boundary. A later revision
        cannot leak backwards into an older market snapshot.
        """
        if lookback_hours <= 0:
            raise ValueError("Event lookback must be positive.")
        with self._session_factory() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id
                    FROM (
                        SELECT DISTINCT ON (source, external_id)
                            id,
                            source,
                            external_id,
                            first_observed_at,
                            revision_index
                        FROM market_event_observations
                        WHERE first_observed_at <= :captured_at
                          AND first_observed_at >= (
                              :captured_at - make_interval(hours => :lookback_hours)
                          )
                        ORDER BY
                            source,
                            external_id,
                            revision_index DESC,
                            first_observed_at DESC
                    ) AS known
                    ORDER BY first_observed_at, source, external_id
                    """
                ),
                {
                    "captured_at": captured_at,
                    "lookback_hours": lookback_hours,
                },
            ).all()
        return [row[0] for row in rows]

    def store_snapshot(self, snapshot: dict[str, object]) -> UUID:
        params = dict(snapshot)
        params["position_state_json"] = _persisted_position_state_json(params)
        with self._session_factory() as session:
            snapshot_id = session.execute(
                text(
                    """
                    INSERT INTO market_snapshots (
                        captured_at,symbol,capture_status,
                        bid,ask,mid,spread,quote_time,quote_age_seconds,
                        session_code,terminal_trade_allowed,
                        position_state_json,order_state_json,cross_market_state_json,
                        provider_state_json,claudy_state_json,data_availability_json,
                        event_observation_ids_json,
                        latest_m1_id,latest_m5_id,latest_m15_id,
                        latest_h1_id,latest_h4_id,latest_d1_id,snapshot_digest
                    )
                    VALUES (
                        :captured_at,:symbol,:capture_status,
                        :bid,:ask,:mid,:spread,:quote_time,:quote_age_seconds,
                        :session_code,:terminal_trade_allowed,
                        CAST(:position_state_json AS jsonb),
                        CAST(:order_state_json AS jsonb),
                        CAST(:cross_market_state_json AS jsonb),
                        CAST(:provider_state_json AS jsonb),
                        CAST(:claudy_state_json AS jsonb),
                        CAST(:data_availability_json AS jsonb),
                        CAST(:event_observation_ids_json AS jsonb),
                        :latest_m1_id,:latest_m5_id,:latest_m15_id,
                        :latest_h1_id,:latest_h4_id,:latest_d1_id,:snapshot_digest
                    )
                    RETURNING id
                    """
                ),
                params,
            ).scalar_one()
            session.commit()
            return snapshot_id

    def store_event_observation(
        self,
        *,
        source: str,
        external_id: str,
        event_type: str,
        published_at: datetime | None,
        first_observed_at: datetime,
        headline: str | None,
        structured_data_json: str,
        raw_payload_json: str,
        payload_digest: str,
    ) -> tuple[UUID, int, bool]:
        """Append a revision when payload changes; identical payloads are idempotent."""
        with self._session_factory() as session:
            existing = session.execute(
                text(
                    """
                    SELECT id, revision_index
                    FROM market_event_observations
                    WHERE source=:source
                      AND external_id=:external_id
                      AND payload_digest=:payload_digest
                    LIMIT 1
                    """
                ),
                {
                    "source": source,
                    "external_id": external_id,
                    "payload_digest": payload_digest,
                },
            ).mappings().first()
            if existing is not None:
                return existing["id"], int(existing["revision_index"]), False

            revision_index = int(
                session.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(revision_index),0) + 1
                        FROM market_event_observations
                        WHERE source=:source AND external_id=:external_id
                        """
                    ),
                    {"source": source, "external_id": external_id},
                ).scalar_one()
            )
            event_id = session.execute(
                text(
                    """
                    INSERT INTO market_event_observations (
                        source,external_id,event_type,published_at,first_observed_at,
                        revision_index,headline,structured_data_json,raw_payload_json,
                        payload_digest
                    )
                    VALUES (
                        :source,:external_id,:event_type,:published_at,:first_observed_at,
                        :revision_index,:headline,
                        CAST(:structured_data_json AS jsonb),
                        CAST(:raw_payload_json AS jsonb),
                        :payload_digest
                    )
                    RETURNING id
                    """
                ),
                {
                    "source": source,
                    "external_id": external_id,
                    "event_type": event_type,
                    "published_at": published_at,
                    "first_observed_at": first_observed_at,
                    "revision_index": revision_index,
                    "headline": headline,
                    "structured_data_json": structured_data_json,
                    "raw_payload_json": raw_payload_json,
                    "payload_digest": payload_digest,
                },
            ).scalar_one()
            session.commit()
            return event_id, revision_index, True
