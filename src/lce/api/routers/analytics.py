"""The analytical read API a frontend integrates against.

Everything here reads a precomputed snapshot. There is one endpoint that
computes - ``POST /scenarios/analyze`` - and it is bounded by the store: a hard
merchant ceiling, a candidate cap, and a lock so two analyses never contend for
the machine at once.

No simulator concepts cross this boundary. There is no tick, no timeline, no
cursor, no play state. A scenario is a *result*: this shock, these merchants
affected, this projected impact, this recommendation, this replayed
counterfactual. How the backend arrived at it is not part of the contract.

Provenance travels with every analytical result rather than only at the top
level, so a client rendering a single scenario can cite its lineage without
holding the whole snapshot.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from lce.logging import get_logger
from lce.snapshot.analysis import analysis_metadata
from lce.snapshot.dashboard import build_dashboard, execution_status
from lce.snapshot.models import (
    DashboardSummary,
    DependencyView,
    ExecutionStatus,
    MerchantView,
    NetworkOverview,
    OfferContract,
    ScenarioSnapshot,
    ScenarioSummary,
    SystemicRankingView,
)
from lce.snapshot.store import MAX_ON_DEMAND_ORIGINS, SnapshotStore, require_store

logger = get_logger(__name__)

# Mounted by the application at "/api/v1".
router = APIRouter(tags=["analytics"])

Store = Annotated[SnapshotStore, Depends(require_store)]


class MerchantDetail(BaseModel):
    """A merchant with the relationships it sits between."""

    model_config = ConfigDict(extra="forbid")

    merchant: MerchantView
    upstream: list[DependencyView]
    downstream: list[DependencyView]
    scenarios_affected_in: list[str]


class AnalyzeRequest(BaseModel):
    """``POST /scenarios/analyze`` - a shock the caller chose."""

    model_config = ConfigDict(extra="forbid")

    merchant_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_ON_DEMAND_ORIGINS,
        description="Origin merchants. Capped so one request cannot fan out.",
    )
    magnitude_multiple: float = Field(
        default=2.0,
        gt=0.0,
        le=10.0,
        description=(
            "Severity as a multiple of the merchant's own liquidity slack, so the "
            "same value means the same severity on a micro merchant and an anchor."
        ),
    )
    onset_hours: float = Field(default=0.0, ge=0.0)
    max_actions: int = Field(default=2, ge=0, le=2)


# ------------------------------------------------------------------- network


@router.get("/network", response_model=NetworkOverview, summary="Network overview")
def network(store: Store) -> NetworkOverview:
    return store.network()


@router.get(
    "/network/merchants",
    response_model=list[MerchantView],
    summary="Merchant profiles with liquidity and exposure",
)
def merchants(
    store: Store,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    vulnerable_only: bool = False,
    sort: Literal["systemic_rank", "exposure", "cover_ratio", "merchant_id"] = "systemic_rank",
) -> list[MerchantView]:
    """Paged merchant list. Sorting is server-side so pages stay coherent."""
    rows = store.merchants()
    if vulnerable_only:
        rows = [m for m in rows if m.vulnerable]

    def key(row: MerchantView):
        match sort:
            case "systemic_rank":
                rank = row.systemic_rank if row.systemic_rank is not None else 10**9
                return (rank, row.merchant_id)
            case "exposure":
                return (-row.payables_in_horizon, row.merchant_id)
            case "cover_ratio":
                # Worst cover first; merchants owing nothing sort last.
                cover = row.cover_ratio if row.cover_ratio is not None else 10**9
                return (cover, row.merchant_id)
            case _:
                return (row.merchant_id,)

    rows = sorted(rows, key=key)
    return rows[offset : offset + limit]


@router.get(
    "/network/merchants/{merchant_id}",
    response_model=MerchantDetail,
    summary="One merchant, with its relationships",
)
def merchant_detail(merchant_id: str, store: Store) -> MerchantDetail:
    relationships = store.dependencies_for(merchant_id)
    return MerchantDetail(
        merchant=store.merchant(merchant_id),
        upstream=relationships["upstream"],
        downstream=relationships["downstream"],
        scenarios_affected_in=[
            s.scenario_id
            for s in store.scenarios()
            if any(a.merchant_id == merchant_id for a in s.affected_merchants)
        ],
    )


@router.get(
    "/network/dependencies",
    response_model=list[DependencyView],
    summary="Estimated merchant relationships, strongest first",
)
def dependencies(
    store: Store, limit: int = Query(default=50, ge=1, le=1000)
) -> list[DependencyView]:
    return store.dependencies()[:limit]


@router.get(
    "/network/systemic-importance",
    response_model=SystemicRankingView,
    summary="Systemic importance ranking, with its baseline correlations",
)
def systemic_importance(
    store: Store, limit: int = Query(default=50, ge=1, le=1000)
) -> SystemicRankingView:
    ranking = store.systemic()
    return ranking.model_copy(
        update={"entries": ranking.entries[:limit], "provenance": store.provenance}
    )


# ------------------------------------------------------------------ scenarios


@router.get(
    "/scenarios",
    response_model=list[ScenarioSummary],
    summary="Analysed scenarios in this snapshot",
)
def scenarios(store: Store) -> list[ScenarioSummary]:
    return store.scenario_summaries()


@router.get(
    "/scenarios/{scenario_id}",
    response_model=ScenarioSnapshot,
    summary="One scenario: impact, recommendation, counterfactual",
)
def scenario(scenario_id: str, store: Store) -> ScenarioSnapshot:
    return store.scenario(scenario_id)


@router.get(
    "/scenarios/{scenario_id}/impact",
    summary="Affected merchants ranked, and the time-to-constraint distribution",
)
def scenario_impact(scenario_id: str, store: Store) -> dict:
    found = store.scenario(scenario_id)
    return {
        "scenario_id": found.scenario_id,
        "projected_impact": found.projected_impact.model_dump(),
        "affected_merchants": [m.model_dump() for m in found.affected_merchants],
        "time_to_constraint": [b.model_dump() for b in found.time_to_constraint],
        "confidence": found.confidence.model_dump(),
        "provenance": found.provenance.model_dump(),
    }


@router.get(
    "/scenarios/{scenario_id}/interventions",
    summary="The recommendation and its ranked alternatives",
)
def scenario_interventions(scenario_id: str, store: Store) -> dict:
    found = store.scenario(scenario_id)
    return {
        "scenario_id": found.scenario_id,
        "recommended": (
            found.recommended_intervention.model_dump()
            if found.recommended_intervention
            else None
        ),
        "alternatives": [a.model_dump() for a in found.alternatives],
        "offer": found.offer.model_dump() if found.offer else None,
        "provenance": found.provenance.model_dump(),
    }


@router.get(
    "/scenarios/{scenario_id}/counterfactual",
    summary="Before and after, both measured by replaying the action",
)
def scenario_counterfactual(scenario_id: str, store: Store) -> dict:
    found = store.scenario(scenario_id)
    return {
        "scenario_id": found.scenario_id,
        "counterfactual": found.counterfactual.model_dump(),
        "confidence": found.confidence.model_dump(),
        "provenance": found.provenance.model_dump(),
    }


@router.post(
    "/scenarios/analyze",
    response_model=ScenarioSnapshot,
    summary="Analyse a shock the caller selects, inside fixed bounds",
)
def analyze(request: AnalyzeRequest, store: Store) -> ScenarioSnapshot:
    """Bounded on-demand analysis.

    Refuses rather than degrading when the network is larger than the on-demand
    ceiling: an API that quietly returns a worse answer under load is harder to
    reason about than one that says the request is out of scope.
    """
    return store.analyze(
        merchant_ids=request.merchant_ids,
        magnitude_multiple=request.magnitude_multiple,
        onset_hours=request.onset_hours,
        max_actions=request.max_actions,
    )


@router.get("/scenarios/analyze/limits", summary="What on-demand analysis will do")
def analyze_limits(store: Store) -> dict:
    return analysis_metadata(store)


# --------------------------------------------------------------------- offers


@router.get(
    "/offers",
    response_model=list[OfferContract],
    summary="Every recommended offer in this snapshot",
)
def offers(store: Store) -> list[OfferContract]:
    return store.offers()


@router.get(
    "/offers/{merchant_id}",
    response_model=OfferContract,
    summary="The recommended offer for one merchant",
)
def offer(merchant_id: str, store: Store) -> OfferContract:
    return store.offer(merchant_id)


# ------------------------------------------------------- dashboard and status


@router.get(
    "/dashboard",
    response_model=DashboardSummary,
    summary="Everything an operations view needs, in one read",
)
def dashboard(
    store: Store,
    top_n: int = Query(default=10, ge=1, le=100),
    probe_execution: bool = True,
) -> DashboardSummary:
    return build_dashboard(store, top_n=top_n, probe_execution=probe_execution)


@router.get(
    "/execution/status",
    response_model=ExecutionStatus,
    summary="What the payment provider can actually do right now",
)
def execution(probe: bool = True) -> ExecutionStatus:
    return execution_status(probe=probe)


@router.get("/snapshot", summary="Identity and integrity of the loaded snapshot")
def snapshot_info(store: Store) -> dict:
    return store.health()
