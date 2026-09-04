"""The inference service: load once, answer many.

Three operations, and they compose into the product claim: predict who breaks,
recommend what to do about it, replay the recommendation to see whether it
worked.

Loading
-------
The artifact is loaded once, in :meth:`InferenceService.__init__`, and held.
Loading per request would put a hash of the weight file and a numpy
deserialisation on the latency path of every call for no benefit - the bundle is
immutable.

Determinism
-----------
Every operation is deterministic given its inputs. The forward pass has no
sampling; the optimiser's evaluations are simulator runs whose common random
numbers are keyed on the simulation config, not on wall-clock or call order. Two
identical requests return byte-identical answers, and a test asserts it.

Nothing here imports training code. The service reaches
:mod:`lce.inference.artifact`, :mod:`lce.inference.predictor`, the feature
builders, the simulator and the optimiser - never the dataset generator, the
corpus builder, or the experiment runner.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lce.config import ObjectiveSettings, get_settings
from lce.domain.enums import PredictorKind, ShockKind
from lce.domain.intervention import Intervention
from lce.domain.prediction import ModelPrediction, NodeExposure
from lce.domain.shock import Shock, ShockComponent
from lce.errors import ModelError, ValidationError
from lce.inference.artifact import ModelArtifact, load_artifact, resolve_artifact
from lce.inference.predictor import (
    ContagionPrediction,
    HazardPredictor,
    NetworkState,
    build_request_window,
)
from lce.intervention.actions import ActionSet, generate_actions
from lce.intervention.evaluate import replay
from lce.intervention.problem import InterventionConstraints, ObjectiveSpec, check_action
from lce.intervention.scalable import greedy_solve
from lce.learning.features import FEATURE_SCHEMA_VERSION
from lce.logging import get_logger
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import SimulationConfig

logger = get_logger(__name__)

API_VERSION = "v1"


def shock_from_components(components: Sequence[dict[str, Any]], *, name: str = "request") -> Shock:
    """Build a shock from request data, rejecting a malformed one explicitly."""
    if not components:
        raise ValidationError("a shock needs at least one component")
    try:
        parsed = [
            ShockComponent(
                merchant_id=c["merchant_id"],
                magnitude=float(c["magnitude"]),
                t=float(c.get("t", 0.0)),
                kind=ShockKind(c.get("kind", ShockKind.MISSED_INBOUND)),
            )
            for c in components
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"malformed shock component: {exc}") from exc
    return Shock(name=name, components=parsed)


def prediction_to_model_prediction(
    prediction: ContagionPrediction, *, horizon_hours: float
) -> ModelPrediction:
    """Adapt the served prediction to the Phase-1 contract the optimiser reads.

    ``expected_hit_t`` is converted back to absolute simulation hours, because
    the candidate generator and the propagator both work on the simulator's
    clock while the served prediction is measured from the observation cutoff.
    """
    exposures = {
        node.merchant_id: NodeExposure(
            merchant_id=node.merchant_id,
            exposure_score=float(min(max(node.probability_constrained, 0.0), 1.0)),
            expected_hit_t=(
                prediction.observation_cutoff + node.expected_time_to_constraint_hours
                if node.expected_time_to_constraint_hours is not None
                else None
            ),
        )
        for node in prediction.nodes
    }
    return ModelPrediction(
        predictor=PredictorKind.TEMPORAL_GNN
        if "gnn" in prediction.model_version
        else PredictorKind.LINEAR_THRESHOLD,
        model_version=prediction.model_version,
        horizon_hours=horizon_hours,
        threshold=prediction.threshold,
        exposures=exposures,
        metadata={"source": "inference_service"},
    )


@dataclass(slots=True)
class Recommendation:
    """A ranked action set and the one the service would take."""

    selected: list[Intervention] = field(default_factory=list)
    ranked: list[dict[str, Any]] = field(default_factory=list)
    expected_disruption_reduction: float = 0.0
    baseline_disruption: float = 0.0
    residual_disruption: float = 0.0
    cost: float = 0.0
    capital_efficiency: float | None = None
    robustness: dict[str, Any] = field(default_factory=dict)
    feasibility: dict[str, Any] = field(default_factory=dict)
    candidate_summary: dict[str, Any] = field(default_factory=dict)
    solver: dict[str, Any] = field(default_factory=dict)
    model_version: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "selected": [
                {
                    "intervention_id": u.intervention_id,
                    "type": str(u.type),
                    "merchant_id": u.merchant_id,
                    "t": u.t,
                    "amount": u.amount,
                    "shift_hours": u.shift_hours,
                    "tranches": u.tranches,
                    "target_obligation_id": u.target_obligation_id,
                    "cost": u.cost,
                    "description": u.describe(),
                    "explanation": u.provenance,
                }
                for u in self.selected
            ],
            "ranked": self.ranked,
            "baseline_disruption": self.baseline_disruption,
            "residual_disruption": self.residual_disruption,
            "expected_disruption_reduction": self.expected_disruption_reduction,
            "cost": self.cost,
            "capital_efficiency": self.capital_efficiency,
            "robustness": self.robustness,
            "feasibility": self.feasibility,
            "candidates": self.candidate_summary,
            "solver": self.solver,
            "latency_ms": round(self.latency_ms, 2),
        }


class InferenceService:
    """Loads one artifact at startup and serves predictions from it."""

    def __init__(
        self,
        artifact_root: Path | None = None,
        *,
        version: str | None = None,
        objective_settings: ObjectiveSettings | None = None,
    ) -> None:
        root = Path(artifact_root) if artifact_root else get_settings().model_artifact_dir
        started = time.perf_counter()
        self.artifact: ModelArtifact = load_artifact(
            resolve_artifact(root, version), expected_schema=FEATURE_SCHEMA_VERSION
        )
        self.predictor = HazardPredictor(self.artifact)
        self.objective_settings = objective_settings
        self.load_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "inference_service_loaded",
            api_version=API_VERSION,
            load_ms=round(self.load_ms, 2),
            **self.artifact.summary(),
        )

    # ------------------------------------------------------------------ health

    @property
    def model_version(self) -> str:
        return self.artifact.model_version

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "api_version": API_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "load_ms": round(self.load_ms, 2),
            **self.artifact.summary(),
        }

    # ----------------------------------------------------------------- predict

    def predict_contagion(
        self,
        state: NetworkState,
        shock: Shock,
        *,
        observation_cutoff: float,
        horizon_hours: float,
    ) -> tuple[ContagionPrediction, float]:
        """Per-node probability of constraint and expected time to it."""
        started = time.perf_counter()
        window = build_request_window(
            state,
            shock=shock,
            observation_cutoff=observation_cutoff,
            horizon_hours=horizon_hours,
        )
        prediction = self.predictor.predict(window)
        latency = (time.perf_counter() - started) * 1000.0
        logger.info(
            "contagion_predicted",
            n_merchants=len(prediction.nodes),
            n_flagged=len(prediction.flagged()),
            model_version=self.model_version,
            latency_ms=round(latency, 2),
        )
        return prediction, latency

    # --------------------------------------------------------------- recommend

    def recommend(
        self,
        state: NetworkState,
        shock: Shock,
        *,
        observation_cutoff: float = 0.0,
        horizon_hours: float = 168.0,
        constraints: InterventionConstraints | None = None,
        objective: ObjectiveSpec | None = None,
        max_candidates: int = 12,
        seed: int = 20250101,
        robust: bool = False,
        n_scenarios: int = 3,
    ) -> Recommendation:
        """Predict, generate feasible candidates, solve, and report the choice.

        The prediction only *proposes*; the simulator decides. Every candidate's
        value here is a real counterfactual run, so the returned reduction is a
        simulated quantity and not the model's own opinion of itself.
        """
        started = time.perf_counter()
        prediction, _ = self.predict_contagion(
            state,
            shock,
            observation_cutoff=observation_cutoff,
            horizon_hours=horizon_hours,
        )

        graph = state.to_graph()
        bounds = constraints or InterventionConstraints(
            horizon_hours=horizon_hours, decision_time=observation_cutoff
        )
        spec = objective or ObjectiveSpec()
        sim_config = SimulationConfig(horizon_hours=horizon_hours, seed=seed)

        action_set: ActionSet = generate_actions(
            graph,
            shock,
            prediction_to_model_prediction(prediction, horizon_hours=horizon_hours),
            constraints=bounds,
            max_candidates=max_candidates,
        )

        evaluator = CounterfactualEvaluator(
            graph=graph, shock=shock, config=sim_config, objective=self.objective_settings
        )
        solved = greedy_solve(
            evaluator,
            action_set.interventions,
            graph,
            constraints=bounds,
            objective=spec,
        )

        robustness: dict[str, Any] = {}
        if robust and action_set.interventions:
            from lce.intervention.robust import (
                UncertaintySpec,
                plans_from_solver,
                robust_select,
            )

            uncertainty = UncertaintySpec(n_scenarios=n_scenarios, seed=seed)
            outcome = robust_select(
                graph,
                plans_from_solver(solved, action_set.interventions, max_singletons=4),
                shock=shock,
                config=sim_config,
                constraints=bounds,
                objective=spec,
                spec=uncertainty,
                objective_settings=self.objective_settings,
            )
            robustness = outcome.to_dict()
            solved.interventions = outcome.chosen.interventions
            solved.disruption = outcome.chosen.nominal_disruption
            solved.cost = outcome.chosen.cost

        ranked = [
            entry.explain()
            | {"selected": entry.intervention.intervention_id
               in {u.intervention_id for u in solved.interventions}}
            for entry in action_set.scored
        ]

        latency = (time.perf_counter() - started) * 1000.0
        recommendation = Recommendation(
            selected=list(solved.interventions),
            ranked=ranked,
            baseline_disruption=solved.baseline_disruption,
            residual_disruption=solved.disruption,
            expected_disruption_reduction=solved.disruption_prevented,
            cost=solved.cost,
            capital_efficiency=(
                solved.disruption_prevented / solved.cost if solved.cost > 0 else None
            ),
            robustness=robustness,
            feasibility=check_action(solved.interventions, graph, bounds).to_dict(),
            candidate_summary={
                "n_generated": action_set.n_generated,
                "n_feasible": action_set.n_feasible,
                "n_retained": len(action_set),
                "rejected_by_constraint": action_set.rejected,
            },
            solver={
                "method": solved.method,
                "status": solved.status,
                "simulations": solved.simulations,
                "runtime_s": round(solved.runtime_s, 4),
                "objective": spec.to_dict(),
                "constraints": bounds.to_dict(),
            },
            model_version=self.model_version,
            latency_ms=latency,
        )
        logger.info(
            "intervention_recommended",
            n_selected=len(recommendation.selected),
            cost=recommendation.cost,
            reduction=recommendation.expected_disruption_reduction,
            latency_ms=round(latency, 2),
        )
        return recommendation

    # ------------------------------------------------------------------ replay

    def replay(
        self,
        state: NetworkState,
        shock: Shock,
        interventions: Sequence[Intervention],
        *,
        horizon_hours: float = 168.0,
        seed: int = 20250101,
    ) -> dict[str, Any]:
        """Run the simulator with and without the action; return both."""
        started = time.perf_counter()
        graph = state.to_graph()
        sim_config = SimulationConfig(horizon_hours=horizon_hours, seed=seed)

        before = replay(
            graph, shock, [], config=sim_config, objective_settings=self.objective_settings,
            run_id="replay:before",
        )
        after = replay(
            graph,
            shock,
            list(interventions),
            config=sim_config,
            objective_settings=self.objective_settings,
            run_id="replay:after",
        )
        cost = sum(u.cost for u in interventions)
        prevented = before.disruption - after.disruption

        return {
            "model_version": self.model_version,
            "before": before.to_dict(),
            "after": after.to_dict(),
            "disruption_prevented": prevented,
            "disruption_reduction_pct": (
                100.0 * prevented / before.disruption if before.disruption > 0 else 0.0
            ),
            "commerce_preserved": before.value_delayed - after.value_delayed,
            "cost": cost,
            "capital_efficiency": prevented / cost if cost > 0 else None,
            "n_interventions": len(interventions),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }


_SERVICE: InferenceService | None = None


def get_service(
    artifact_root: Path | None = None, *, version: str | None = None
) -> InferenceService:
    """Process-wide singleton, built on first use.

    The API depends on this rather than constructing a service per request: the
    artifact is immutable, and re-reading it on every call would add a file hash
    to every request's latency for nothing.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = InferenceService(artifact_root, version=version)
    return _SERVICE


def reset_service() -> None:
    """Drop the cached service. Tests use it; production should not need it."""
    global _SERVICE
    _SERVICE = None


def require_service() -> InferenceService:
    """The API dependency: a loaded service, or a clear error explaining why not."""
    try:
        return get_service()
    except (ModelError, FileNotFoundError) as exc:  # pragma: no cover - config error
        raise ModelError(
            "no servable model artifact is available; export one with "
            "'lce infer export' before starting the inference API",
            reason=str(exc),
        ) from exc
