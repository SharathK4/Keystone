"""Shock, simulation, prediction, intervention and evaluation endpoints.

These trace the demo narrative in order: create a shock, simulate it, predict
where it spreads, ask where to intervene, apply the plan, then rank systemic
importance.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from lce.api.deps import Analysis, Networks
from lce.api.schemas import (
    CascadeResponse,
    CreateShockRequest,
    EvaluateRequest,
    EvaluationResponse,
    InterventionView,
    NodeExposureView,
    NodeOutcomeView,
    OptimizeRequest,
    OptimizeResponse,
    PredictionResponse,
    PredictRequest,
    PropagationEventView,
    ShockResponse,
    SimulateRequest,
    SystemicImportanceRequest,
    SystemicImportanceResponse,
)
from lce.domain.propagation import CascadeResult
from lce.domain.shock import Shock, ShockComponent
from lce.errors import NotFoundError, ValidationError
from lce.models.propagation import PropagationConfig
from lce.optimization.candidates import CandidateConfig
from lce.optimization.search import SearchConfig
from lce.simulation.engine import SimulationConfig

router = APIRouter(tags=["analysis"])

TIMELINE_SLICES = (6.0, 24.0, 48.0, 72.0)


def _shock_response(shock: Shock) -> ShockResponse:
    return ShockResponse(
        shock_id=shock.shock_id,
        name=shock.name,
        description=shock.description,
        total_magnitude=shock.total_magnitude,
        onset_t=shock.onset_t,
        origin_ids=shock.origin_ids,
        components=[c.model_dump(mode="json") for c in shock.components],
    )


def _cascade_response(result: CascadeResult, include_outcomes: bool) -> CascadeResponse:
    outcomes = []
    if include_outcomes:
        outcomes = [
            NodeOutcomeView(
                merchant_id=o.merchant_id,
                is_affected=o.is_affected,
                became_constrained=o.became_constrained,
                became_defaulted=o.became_defaulted,
                first_constrained_t=o.first_constrained_t,
                hop_distance=o.hop_distance,
                value_delayed=o.value_delayed,
                weighted_delay=o.weighted_delay,
                defaults_caused=o.defaults_caused,
                min_buffer=o.min_buffer,
                final_status=str(o.final_status),
            )
            # Only the affected nodes: a 2000-merchant network would otherwise
            # return 2000 rows of "nothing happened".
            for o in sorted(result.outcomes.values(), key=lambda x: x.merchant_id)
            if o.is_affected
        ]

    return CascadeResponse(
        run_id=result.run_id,
        shock_id=result.shock_id,
        plan_id=result.plan_id,
        horizon_hours=result.horizon_hours,
        disruption=result.disruption,
        disruption_breakdown=result.disruption_breakdown,
        summary=result.summary(),
        affected_ids=result.affected_ids,
        downstream_affected_ids=result.downstream_affected_ids,
        timeline={
            f"{t:.0f}h": result.affected_by(t)
            for t in TIMELINE_SLICES
            if t <= result.horizon_hours
        },
        outcomes=outcomes,
    )


def _sim_config(
    horizon_hours: float | None = None,
    tick_hours: float | None = None,
    seed: int | None = None,
) -> SimulationConfig:
    config = SimulationConfig.from_settings()
    updates: dict[str, Any] = {}
    if horizon_hours is not None:
        updates["horizon_hours"] = horizon_hours
    if tick_hours is not None:
        updates["tick_hours"] = tick_hours
    if seed is not None:
        updates["seed"] = seed
    return replace(config, **updates) if updates else config


# --------------------------------------------------------------------- shocks


@router.post(
    "/datasets/{dataset_id}/shocks",
    response_model=ShockResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Define a liquidity shock scenario",
)
def create_shock(
    dataset_id: str, request: CreateShockRequest, analysis: Analysis, networks: Networks
) -> ShockResponse:
    graph = networks.load_graph(dataset_id)
    unknown = [c.merchant_id for c in request.components if not graph.has_merchant(c.merchant_id)]
    if unknown:
        raise ValidationError(f"unknown merchants in shock: {unknown}")

    shock = Shock(
        name=request.name or f"shock:{dataset_id}",
        description=request.description,
        components=[
            ShockComponent(
                merchant_id=c.merchant_id,
                magnitude=c.magnitude,
                t=c.t,
                kind=c.kind,
                duration_hours=c.duration_hours,
                target_obligation_id=c.target_obligation_id,
            )
            for c in request.components
        ],
    )
    analysis.save_shock(shock, dataset_id)
    return _shock_response(shock)


@router.get(
    "/datasets/{dataset_id}/shocks",
    response_model=list[ShockResponse],
    summary="List shock scenarios",
)
def list_shocks(
    dataset_id: str, analysis: Analysis, limit: int = Query(default=50, ge=1, le=200)
) -> list[ShockResponse]:
    return [_shock_response(s) for s in analysis.uow.shocks.list_for_dataset(dataset_id, limit)]


@router.get("/shocks/{shock_id}", response_model=ShockResponse, summary="Shock detail")
def get_shock(shock_id: str, analysis: Analysis) -> ShockResponse:
    return _shock_response(analysis.get_shock(shock_id))


# ----------------------------------------------------------------- simulation


@router.post(
    "/datasets/{dataset_id}/simulate",
    response_model=CascadeResponse,
    summary="Simulate a shock (omit shock_id for the undisturbed baseline)",
)
def simulate(
    dataset_id: str,
    request: SimulateRequest,
    analysis: Analysis,
    include_outcomes: bool = Query(default=True),
) -> CascadeResponse:
    shock = analysis.get_shock(request.shock_id) if request.shock_id else None
    plan = analysis.uow.plans.require(request.plan_id) if request.plan_id else None

    result = analysis.simulate(
        dataset_id,
        shock,
        plan=plan,
        config=_sim_config(request.horizon_hours, request.tick_hours, request.seed),
        estimator=request.estimator,
        store_events=request.store_events,
    )
    return _cascade_response(result, include_outcomes)


@router.get("/runs/{run_id}/cascade", summary="Stored cascade outcomes for a run")
def get_cascade(run_id: str, analysis: Analysis) -> dict[str, Any]:
    return analysis.get_cascade(run_id)


@router.get(
    "/runs/{run_id}/events",
    response_model=list[PropagationEventView],
    summary="Causally-linked propagation events for a run",
)
def get_events(
    run_id: str,
    analysis: Analysis,
    merchant_id: str | None = None,
    limit: int = Query(default=500, ge=1, le=10000),
) -> list[PropagationEventView]:
    events = analysis.uow.cascades.events_for_run(
        run_id, merchant_id=merchant_id, limit=limit
    )
    if not events and analysis.uow.runs.get(run_id) is None:
        raise NotFoundError(f"unknown run {run_id!r}", run_id=run_id)
    return [
        PropagationEventView(
            event_id=e.event_id,
            sequence=e.sequence,
            t=e.t,
            type=str(e.type),
            merchant_id=e.merchant_id,
            counterparty_id=e.counterparty_id,
            amount=e.amount,
            hop=e.hop,
            caused_by=e.caused_by,
            status_after=str(e.status_after) if e.status_after else None,
        )
        for e in events
    ]


# ----------------------------------------------------------------- prediction


@router.post(
    "/datasets/{dataset_id}/predict",
    response_model=PredictionResponse,
    summary="Predict which merchants a shock will reach, and when",
)
def predict(
    dataset_id: str,
    request: PredictRequest,
    analysis: Analysis,
    top: int = Query(default=50, ge=1, le=1000),
) -> PredictionResponse:
    shock = analysis.get_shock(request.shock_id)
    config = replace(
        PropagationConfig(),
        horizon_hours=request.horizon_hours,
        threshold=request.threshold,
        max_hops=request.max_hops,
    )
    prediction = analysis.predict(
        dataset_id,
        shock,
        predictor=request.predictor,
        config=config,
        estimator=request.estimator,
    )
    return PredictionResponse(
        prediction_id=prediction.prediction_id,
        shock_id=prediction.shock_id,
        predictor=str(prediction.predictor),
        model_version=prediction.model_version,
        horizon_hours=prediction.horizon_hours,
        threshold=prediction.threshold,
        inference_ms=prediction.inference_ms,
        predicted_affected_ids=prediction.predicted_affected_ids,
        exposures=[
            NodeExposureView(
                merchant_id=e.merchant_id,
                exposure_score=e.exposure_score,
                expected_shortfall=e.expected_shortfall,
                expected_hit_t=e.expected_hit_t,
                hop_distance=e.hop_distance,
                contributing_sources=e.contributing_sources,
            )
            for e in prediction.ranked(limit=top)
        ],
    )


# --------------------------------------------------------------- intervention


@router.post(
    "/datasets/{dataset_id}/interventions/optimize",
    response_model=OptimizeResponse,
    summary="Find the cheapest intervention that most reduces disruption",
)
def optimize(
    dataset_id: str, request: OptimizeRequest, analysis: Analysis
) -> OptimizeResponse:
    shock = analysis.get_shock(request.shock_id)
    sim_config = _sim_config(horizon_hours=request.horizon_hours)

    try:
        result = analysis.optimize(
            dataset_id,
            shock,
            optimizer=request.optimizer,
            search_config=replace(
                SearchConfig(),
                budget=request.budget,
                max_actions=request.max_actions,
                lazy=request.lazy,
            ),
            candidate_config=replace(
                CandidateConfig(),
                top_k_nodes=request.top_k_nodes,
                max_candidates=request.max_candidates,
            ),
            simulation_config=sim_config,
            estimator=request.estimator,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        ) from exc

    dpr = result.disruption_prevented_per_rupee
    return OptimizeResponse(
        plan_id=result.plan.plan_id,
        optimizer=str(result.optimizer),
        interventions=[
            InterventionView(
                intervention_id=u.intervention_id,
                type=u.type,
                merchant_id=u.merchant_id,
                t=u.t,
                amount=u.amount,
                shift_hours=u.shift_hours,
                tranches=u.tranches,
                target_obligation_id=u.target_obligation_id,
                cost=u.cost,
                description=u.describe(),
            )
            for u in result.plan.interventions
        ],
        total_cost=result.cost,
        baseline_disruption=result.baseline_disruption,
        achieved_disruption=result.achieved_disruption,
        disruption_prevented=result.disruption_prevented,
        # inf is not JSON-representable; a free-but-effective plan reports null.
        disruption_prevented_per_rupee=dpr if dpr != float("inf") else None,
        candidates_considered=result.candidates_considered,
        simulations_run=result.simulations_run,
        elapsed_ms=result.elapsed_ms,
        notes=result.notes,
    )


# ------------------------------------------------------------------ analysis


@router.post(
    "/datasets/{dataset_id}/evaluate",
    response_model=EvaluationResponse,
    summary="Score a prediction against a simulation of the truth",
)
def evaluate(
    dataset_id: str, request: EvaluateRequest, analysis: Analysis
) -> EvaluationResponse:
    shock = analysis.get_shock(request.shock_id)
    prediction = analysis.uow.predictions.require(request.prediction_id)
    evaluation = analysis.evaluate(
        dataset_id,
        shock,
        prediction=prediction,
        simulation_config=_sim_config(horizon_hours=request.horizon_hours),
        estimator=request.estimator,
    )
    return EvaluationResponse(
        evaluation_id=evaluation.evaluation_id,
        name=evaluation.name,
        predictor=evaluation.predictor,
        optimizer=evaluation.optimizer,
        headline=evaluation.headline(),
        by_horizon={
            k: {
                "precision": v.precision,
                "recall": v.recall,
                "f1": v.f1,
                "support": float(v.support),
            }
            for k, v in evaluation.by_horizon.items()
        },
    )


@router.post(
    "/datasets/{dataset_id}/systemic-importance",
    response_model=SystemicImportanceResponse,
    summary="Rank merchants by how much damage a shock at them causes",
)
def systemic_importance(
    dataset_id: str, request: SystemicImportanceRequest, analysis: Analysis
) -> SystemicImportanceResponse:
    """One simulation per merchant - the most expensive endpoint in the API.

    Use ``limit`` on large networks.
    """
    ranking = analysis.systemic_importance(
        dataset_id,
        shock_fraction=request.shock_fraction,
        limit=request.limit,
        estimator=request.estimator,
    )
    payload = ranking.to_dict()
    return SystemicImportanceResponse(
        dataset_id=dataset_id,
        baseline_disruption=payload["baseline_disruption"],
        shock_fraction=payload["shock_fraction"],
        ranking=payload["ranking"],
    )
