"""Provider Intelligence Day 18 combined-book research harness.

Day 18 is intentionally non-executable. It models XAUUSD book aggregation and
portfolio choices, but cannot alter live/paper execution. Literal BUY+SELL coexistence
is permitted only when a read-only MT5 account observation proves hedging mode.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.metaapi_read_gateway import MetaApiReadGateway
from app.models import AuditEvent
from app.mt5_crypto import BrokerCredentialDecryptionError, MetaApiTokenCipher
from app.provider_day17_confidence_sizing import HeatCaps, HeatLeg, allocate_research_heat, summarize_heat

MODEL_VERSION = "provider_day18_v1"
PORTFOLIO_AUTHORITY = "DORMANT_RESEARCH_ONLY"
PRODUCTION_PORTFOLIO_AUTHORITY = False
ACCOUNT_MODE_REQUIRED = True

_HEDGING_VALUES = {
    "RETAIL_HEDGING",
    "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING",
}
_NETTING_VALUES = {
    "RETAIL_NETTING",
    "ACCOUNT_MARGIN_MODE_RETAIL_NETTING",
    "EXCHANGE",
    "ACCOUNT_MARGIN_MODE_EXCHANGE",
}


@dataclass(frozen=True, slots=True)
class BookLeg:
    provider_id: str
    cluster_id: str
    direction: str
    risk_fraction: float
    horizon_low_minutes: float
    horizon_high_minutes: float

    def validate(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id_required")
        if not self.cluster_id:
            raise ValueError("cluster_id_required")
        if self.direction not in {"BUY", "SELL"}:
            raise ValueError("direction_must_be_BUY_or_SELL")
        if not math.isfinite(self.risk_fraction) or self.risk_fraction < 0:
            raise ValueError("risk_fraction_must_be_finite_nonnegative")
        if (
            not math.isfinite(self.horizon_low_minutes)
            or not math.isfinite(self.horizon_high_minutes)
            or self.horizon_low_minutes < 0
            or self.horizon_high_minutes < self.horizon_low_minutes
        ):
            raise ValueError("invalid_horizon_interval")


@dataclass(frozen=True, slots=True)
class ResearchCandidate:
    provider_id: str
    cluster_id: str
    direction: str
    posterior_edge_r: float
    horizon_low_minutes: float
    horizon_high_minutes: float
    eligible_from_prior_gate: bool

    def validate(self) -> None:
        if not self.provider_id or not self.cluster_id:
            raise ValueError("candidate_identity_required")
        if self.direction not in {"BUY", "SELL"}:
            raise ValueError("direction_must_be_BUY_or_SELL")
        if not math.isfinite(self.posterior_edge_r):
            raise ValueError("posterior_edge_must_be_finite")
        if (
            not math.isfinite(self.horizon_low_minutes)
            or not math.isfinite(self.horizon_high_minutes)
            or self.horizon_low_minutes < 0
            or self.horizon_high_minutes < self.horizon_low_minutes
        ):
            raise ValueError("invalid_horizon_interval")


def normalize_margin_mode(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in _HEDGING_VALUES:
        return "hedging"
    if normalized in _NETTING_VALUES:
        return "netting"
    return "unknown"


def horizons_compatible(
    left_low: float,
    left_high: float,
    right_low: float,
    right_high: float,
) -> bool:
    values = (left_low, left_high, right_low, right_high)
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("invalid_horizon_interval")
    if left_high < left_low or right_high < right_low:
        raise ValueError("invalid_horizon_interval")
    return max(left_low, right_low) <= min(left_high, right_high)


def aggregate_cluster_research(candidates: Iterable[ResearchCandidate]) -> list[ResearchCandidate]:
    """Down-weight relay copies by retaining one strongest eligible candidate per cluster/side."""
    selected: dict[tuple[str, str], ResearchCandidate] = {}
    for candidate in candidates:
        candidate.validate()
        if not candidate.eligible_from_prior_gate:
            continue
        key = (candidate.cluster_id, candidate.direction)
        incumbent = selected.get(key)
        if incumbent is None or (candidate.posterior_edge_r, candidate.provider_id) > (
            incumbent.posterior_edge_r,
            incumbent.provider_id,
        ):
            selected[key] = candidate
    return [selected[key] for key in sorted(selected)]


def book_state(open_legs: Iterable[BookLeg], *, account_mode: str) -> dict[str, object]:
    legs = list(open_legs)
    for leg in legs:
        leg.validate()
    if account_mode not in {"hedging", "netting", "unknown"}:
        raise ValueError("invalid_account_mode")

    heat_legs = [HeatLeg(leg.direction, leg.risk_fraction, leg.cluster_id) for leg in legs]
    heat = summarize_heat(heat_legs)
    signed_net = float(heat["signed_net_heat_fraction"])
    if signed_net > 1e-12:
        collapsed_direction = "BUY"
    elif signed_net < -1e-12:
        collapsed_direction = "SELL"
    else:
        collapsed_direction = "FLAT"

    if account_mode == "netting":
        broker_equivalent_positions = 0 if collapsed_direction == "FLAT" else 1
        provider_attribution_preserved_at_broker_position_level = False
        literal_hedge_supported = False
    elif account_mode == "hedging":
        broker_equivalent_positions = len([leg for leg in legs if leg.risk_fraction > 0])
        provider_attribution_preserved_at_broker_position_level = True
        literal_hedge_supported = True
    else:
        broker_equivalent_positions = None
        provider_attribution_preserved_at_broker_position_level = False
        literal_hedge_supported = False

    return {
        "model_version": MODEL_VERSION,
        "account_mode": account_mode,
        "heat": heat,
        "provider_count": len({leg.provider_id for leg in legs}),
        "cluster_count": len({leg.cluster_id for leg in legs}),
        "broker_equivalent_positions": broker_equivalent_positions,
        "netting_collapsed_direction": collapsed_direction,
        "literal_hedge_supported": literal_hedge_supported,
        "provider_attribution_preserved_at_broker_position_level": provider_attribution_preserved_at_broker_position_level,
        "production_portfolio_authority": PRODUCTION_PORTFOLIO_AUTHORITY,
    }


def research_book_decision(
    *,
    candidates: Iterable[ResearchCandidate],
    account_mode: str,
) -> dict[str, object]:
    """Return a dormant research choice: BUY-only, SELL-only, both, reduced or none."""
    if account_mode not in {"hedging", "netting", "unknown"}:
        raise ValueError("invalid_account_mode")
    selected = aggregate_cluster_research(candidates)
    buys = [item for item in selected if item.direction == "BUY" and item.posterior_edge_r > 0]
    sells = [item for item in selected if item.direction == "SELL" and item.posterior_edge_r > 0]
    buy_score = sum(item.posterior_edge_r for item in buys)
    sell_score = sum(item.posterior_edge_r for item in sells)

    if not buys and not sells:
        choice = "none"
        reason = "no_prior_gate_eligible_positive_edge_candidate"
    elif buys and not sells:
        choice = "buy_only"
        reason = "eligible_buy_evidence_only"
    elif sells and not buys:
        choice = "sell_only"
        reason = "eligible_sell_evidence_only"
    else:
        cross_horizon_compatible = any(
            horizons_compatible(
                buy.horizon_low_minutes,
                buy.horizon_high_minutes,
                sell.horizon_low_minutes,
                sell.horizon_high_minutes,
            )
            for buy in buys
            for sell in sells
        )
        if account_mode == "hedging" and cross_horizon_compatible:
            choice = "both"
            reason = "hedging_mode_proven_and_opposing_horizons_compatible"
        elif account_mode == "unknown":
            choice = "none"
            reason = "account_mode_unproven_blocks_combined_book"
        else:
            stronger = "BUY" if buy_score >= sell_score else "SELL"
            stronger_score = max(buy_score, sell_score)
            weaker_score = min(buy_score, sell_score)
            conflict_ratio = 0.0 if stronger_score <= 0 else weaker_score / stronger_score
            if conflict_ratio >= 0.75:
                choice = "reduced"
                reason = f"opposing_evidence_conflict_netting_or_incompatible_horizon:{stronger}"
            else:
                choice = "buy_only" if stronger == "BUY" else "sell_only"
                reason = "dominant_side_after_opposing_evidence_netting_constraint"

    return {
        "model_version": MODEL_VERSION,
        "choice": choice,
        "reason": reason,
        "account_mode": account_mode,
        "buy_cluster_adjusted_score_r": round(buy_score, 8),
        "sell_cluster_adjusted_score_r": round(sell_score, 8),
        "selected_candidate_count": len(selected),
        "both_branch_allowed": choice == "both",
        "production_portfolio_authority": PRODUCTION_PORTFOLIO_AUTHORITY,
        "authority": PORTFOLIO_AUTHORITY,
    }


def research_heat_allocation(
    *,
    open_legs: Iterable[BookLeg],
    candidate: BookLeg,
    caps: HeatCaps,
) -> dict[str, object]:
    candidate.validate()
    legs = list(open_legs)
    for leg in legs:
        leg.validate()
    return allocate_research_heat(
        open_legs=[HeatLeg(leg.direction, leg.risk_fraction, leg.cluster_id) for leg in legs],
        candidate=HeatLeg(candidate.direction, candidate.risk_fraction, candidate.cluster_id),
        caps=caps,
    )


async def observe_connected_mt5_account_mode(
    *,
    session_factory: sessionmaker[Session],
    cipher: MetaApiTokenCipher,
    gateway: MetaApiReadGateway,
) -> dict[str, object]:
    """Read and persist the connected account's MT5 margin mode. No trade mutation occurs."""
    with session_factory() as session:
        row = session.execute(
            text(
                """
                SELECT id, metaapi_account_id, metaapi_token_ciphertext, account_environment
                FROM mt5_accounts
                WHERE status='connected' AND metaapi_account_id IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT 1
                """
            )
        ).mappings().first()
    if row is None:
        return {"observed": False, "reason": "connected_mt5_account_not_found"}

    try:
        token = cipher.decrypt(bytes(row["metaapi_token_ciphertext"]))
    except BrokerCredentialDecryptionError:
        return {"observed": False, "reason": "stored_metaapi_token_unreadable"}

    region = await gateway.resolve_account_region(token=token, account_id=str(row["metaapi_account_id"]))
    info = await gateway.read_account_information(
        token=token,
        account_id=str(row["metaapi_account_id"]),
        region=region,
    )
    raw_mode = info.get("marginMode")
    normalized = normalize_margin_mode(raw_mode)
    payload = {
        "model_version": MODEL_VERSION,
        "account_mode": normalized,
        "metaapi_margin_mode": str(raw_mode or ""),
        "account_environment": str(row["account_environment"] or ""),
        "literal_hedge_supported": normalized == "hedging",
        "trade_action_created": False,
        "production_portfolio_authority": False,
    }
    with session_factory() as session:
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_type="provider_intelligence.day18_account_mode",
                entity_type="mt5_account",
                entity_id=UUID(str(row["id"])),
                payload=payload,
            )
        )
        session.commit()
    return {"observed": True, **payload}


def engineering_acceptance_snapshot() -> dict[str, object]:
    netting = book_state(
        [
            BookLeg("p1", "c1", "BUY", 0.01, 5, 30),
            BookLeg("p2", "c2", "SELL", 0.004, 5, 30),
        ],
        account_mode="netting",
    )
    hedging = book_state(
        [
            BookLeg("p1", "c1", "BUY", 0.01, 5, 30),
            BookLeg("p2", "c2", "SELL", 0.004, 5, 30),
        ],
        account_mode="hedging",
    )
    return {
        "model_version": MODEL_VERSION,
        "engineering_harness_green": (
            netting["broker_equivalent_positions"] == 1
            and netting["literal_hedge_supported"] is False
            and hedging["broker_equivalent_positions"] == 2
            and hedging["provider_attribution_preserved_at_broker_position_level"] is True
        ),
        "production_portfolio_authority": PRODUCTION_PORTFOLIO_AUTHORITY,
        "account_mode_required": ACCOUNT_MODE_REQUIRED,
    }
