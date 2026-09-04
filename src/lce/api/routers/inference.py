"""Versioned inference endpoints - the surface a frontend integrates against.

Three routes, deliberately narrow:

``POST /api/v1/predict/contagion``        who becomes liquidity-constrained, and when
``POST /api/v1/interventions/recommend``  what to do about it, ranked
``POST /api/v1/scenarios/replay``         what actually happens if you do it
``GET  /api/v1/model``                     which artifact is loaded

These are the *serving* API. The research endpoints under ``/api/v1/datasets``
generate networks, learn dependencies and run experiments; nothing here does any
of that. A deployment can run this router with a model artifact and no training
code present at all, which is the point.

The service is a process-wide singleton resolved through a dependency, so the
artifact is read once at first use rather than per request. Domain errors raised
below are mapped to HTTP status codes by the application's error handler, so
routes stay free of try/except.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from lce.domain.intervention import Intervention
from lce.inference.predictor import NetworkState, merchant_from_payload
from lce.inference.schemas import (
    ContagionRequest,
    ContagionResponse,
    RecommendRequest,
    RecommendResponse,
    ReplayRequest,
    ReplayResponse,
)
from lce.inference.service import InferenceService, require_service, shock_from_components
from lce.intervention.problem import InterventionConstraints, ObjectiveSpec
from lce.logging import get_logger

logger = get_logger(__name__)

# The application mounts this at "/api/v1"; the paths below are relative to it.
router = APIRouter(tags=["inference"])

Service = Annotated[InferenceService, Depends(require_service)]


def _state(payload) -> NetworkState:  # type: ignore[no-untyped-def]
    """Coerce a validated request network into the serving state object."""
    from lce.domain.events import Obligation, PaymentEvent

    return NetworkState(
        network_id=payload.network_id,
        merchants=[merchant_from_payload(m.model_dump()) for m in payload.merchants],
        obligations=[
            Obligation(
                obligation_id=o.obligation_id,
                debtor_id=o.debtor_id,
                creditor_id=o.creditor_id,
                amount=o.amount,
                issued_t=o.issued_t,
                due_t=o.due_t,
                priority=o.priority,
                amount_paid=o.amount_paid,
            )
            for o in payload.obligations
        ],
        payments=[
            PaymentEvent(
                payer_id=p.payer_id,
                payee_id=p.payee_id,
                amount=p.amount,
                t=p.t,
                obligation_id=p.obligation_id,
            )
            for p in payload.payments
        ],
    )


@router.get("/model", summary="Which artifact is loaded")
def model_info(service: Service) -> dict:
    """Artifact identity and integrity - what any answer below came from."""
    return service.health()


@router.post(
    "/predict/contagion",
    response_model=ContagionResponse,
    summary="Per-merchant probability of becoming liquidity-constrained",
)
def predict_contagion(request: ContagionRequest, service: Service) -> ContagionResponse:
    """Predict who breaks and when, from observable state only.

    The observation cutoff is enforced server-side: payments at or after it are
    dropped before any feature is computed, so a caller cannot accidentally (or
    deliberately) obtain a prediction that used the future.
    """
    prediction, latency = service.predict_contagion(
        _state(request.network),
        shock_from_components(
            [c.model_dump() for c in request.shock.components], name=request.shock.name
        ),
        observation_cutoff=request.observation_cutoff,
        horizon_hours=request.horizon_hours,
    )
    return ContagionResponse(**prediction.to_dict(), latency_ms=round(latency, 2))


@router.post(
    "/interventions/recommend",
    response_model=RecommendResponse,
    summary="Ranked interventions and the recommended action",
)
def recommend(request: RecommendRequest, service: Service) -> RecommendResponse:
    """Rank feasible actions and return the one the optimiser would take.

    Every number in the response is simulated, not predicted: the prediction
    chooses what to *consider*, and the counterfactual simulator decides what the
    expected reduction actually is.
    """
    constraints = InterventionConstraints(
        budget=request.constraints.budget,
        max_actions=request.constraints.max_actions,
        max_per_merchant=request.constraints.max_per_merchant,
        max_extension_hours=request.constraints.max_extension_hours,
        max_acceleration_hours=request.constraints.max_acceleration_hours,
        max_tranches=request.constraints.max_tranches,
        min_amount=request.constraints.min_amount,
        decision_time=request.observation_cutoff,
        horizon_hours=request.horizon_hours,
    )
    objective = ObjectiveSpec(
        form=request.objective.form,
        lam=request.objective.lam,
        epsilon=request.objective.epsilon,
        epsilon_fraction=request.objective.epsilon_fraction,
    )
    result = service.recommend(
        _state(request.network),
        shock_from_components(
            [c.model_dump() for c in request.shock.components], name=request.shock.name
        ),
        observation_cutoff=request.observation_cutoff,
        horizon_hours=request.horizon_hours,
        constraints=constraints,
        objective=objective,
        max_candidates=request.max_candidates,
        seed=request.seed,
        robust=request.robust,
        n_scenarios=request.n_scenarios,
    )
    return RecommendResponse(**result.to_dict())


@router.post(
    "/scenarios/replay",
    response_model=ReplayResponse,
    summary="Execute an intervention in the simulator and return before/after",
)
def replay_scenario(request: ReplayRequest, service: Service) -> ReplayResponse:
    """Replay a chosen action. The number that settles whether it worked."""
    interventions = [
        Intervention(
            type=item.type,
            merchant_id=item.merchant_id,
            t=item.t,
            amount=item.amount,
            shift_hours=item.shift_hours,
            tranches=item.tranches,
            target_obligation_id=item.target_obligation_id,
        )
        for item in request.interventions
    ]
    return ReplayResponse(
        **service.replay(
            _state(request.network),
            shock_from_components(
                [c.model_dump() for c in request.shock.components], name=request.shock.name
            ),
            interventions,
            horizon_hours=request.horizon_hours,
            seed=request.seed,
        )
    )
