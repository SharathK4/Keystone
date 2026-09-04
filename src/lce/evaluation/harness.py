"""End-to-end evaluation.

Ties the pieces together into the two measurements this project actually claims:

1. **Contagion prediction** - does the predictor identify the merchants a shock
   will reach, and when? Scored against the simulator's shock-attributable
   affected set.
2. **Intervention search** - does the optimiser find a cheap plan, and how far
   is it from the best plan available? Scored against an exhaustive optimum
   where one is affordable to compute.

Attribution
-----------
Both measurements difference against the **no-shock baseline**. The undisturbed
network already carries some habitual lateness and a few stressed merchants;
crediting a model for predicting those would inflate every number without
measuring anything about contagion. Every headline figure here is therefore a
*marginal* quantity: what the shock caused, and what the intervention prevented.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from lce.config import ObjectiveSettings
from lce.domain.evaluation import EvaluationResult
from lce.domain.prediction import ModelPrediction
from lce.domain.propagation import CascadeResult
from lce.domain.shock import Shock
from lce.evaluation.metrics import (
    attributable_affected,
    classification_metrics,
    intervention_metrics,
    timing_metrics,
)
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.optimization.search import SearchConfig, SearchResult
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import LiquiditySimulator, SimulationConfig

logger = get_logger(__name__)

DEFAULT_HORIZON_SLICES: tuple[float, ...] = (6.0, 24.0, 48.0, 72.0)


@dataclass(slots=True)
class GroundTruth:
    """Simulated reality for one shock, with its no-shock counterfactual."""

    shock: Shock
    baseline: CascadeResult
    shocked: CascadeResult
    horizon_hours: float

    @property
    def affected(self) -> list[str]:
        """Shock-attributable affected set A(G,S) \\ A(G,0)."""
        return attributable_affected(self.shocked.affected_ids, self.baseline.affected_ids)

    @property
    def hit_times(self) -> dict[str, float]:
        attributable = set(self.affected)
        return {
            m: t for m, t in self.shocked.hit_times().items() if m in attributable
        }

    @property
    def attributable_disruption(self) -> float:
        return max(0.0, (self.shocked.disruption or 0.0) - (self.baseline.disruption or 0.0))

    def affected_by(self, t: float) -> list[str]:
        attributable = set(self.affected)
        return sorted(m for m in self.shocked.affected_by(t) if m in attributable)

    def summary(self) -> dict[str, Any]:
        return {
            "n_affected": len(self.affected),
            "n_defaulted": len(self.shocked.defaulted_ids),
            "max_hop": self.shocked.max_hop(),
            "attributable_disruption": self.attributable_disruption,
            "baseline_disruption": self.baseline.disruption,
            "shocked_disruption": self.shocked.disruption,
        }


def build_ground_truth(
    graph: TemporalPaymentGraph,
    shock: Shock,
    *,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
    baseline: CascadeResult | None = None,
) -> GroundTruth:
    """Simulate the shock and (unless supplied) its no-shock counterfactual.

    ``baseline`` is accepted so a sweep over many shocks on one network computes
    it once rather than once per shock - it does not depend on the shock.
    """
    sim_config = config or SimulationConfig()
    base = baseline or LiquiditySimulator(graph, sim_config, objective).run(
        None, run_id="baseline"
    )
    shocked = LiquiditySimulator(graph, sim_config, objective).run(shock, run_id="shocked")
    return GroundTruth(
        shock=shock,
        baseline=base,
        shocked=shocked,
        horizon_hours=sim_config.horizon_hours,
    )


def evaluate_prediction(
    prediction: ModelPrediction,
    truth: GroundTruth,
    graph: TemporalPaymentGraph,
    *,
    name: str = "",
    dataset_version: str | None = None,
    horizon_slices: tuple[float, ...] = DEFAULT_HORIZON_SLICES,
    seed: int | None = None,
) -> EvaluationResult:
    """Score a contagion prediction against simulated reality."""
    universe = graph.merchant_ids
    affected = truth.affected
    scores = prediction.scores()

    classification = classification_metrics(
        scores, affected, universe=universe, threshold=prediction.threshold
    )
    timing = timing_metrics(
        prediction.hit_times(), truth.hit_times, restrict_to=affected
    )

    # Sliced by prediction horizon - the 6h / 24h / 48h / 72h view the demo
    # walks through. Each slice is scored against what had actually happened by
    # that time, not against the final state.
    by_horizon = {}
    for t in horizon_slices:
        if t > truth.horizon_hours:
            continue
        by_horizon[f"{t:.0f}h"] = classification_metrics(
            {
                m: (
                    scores.get(m, 0.0)
                    if (
                        prediction.exposures.get(m) is not None
                        and (prediction.exposures[m].expected_hit_t or float("inf")) <= t
                    )
                    else 0.0
                )
                for m in universe
            },
            truth.affected_by(t),
            universe=universe,
            threshold=prediction.threshold,
        )

    result = EvaluationResult(
        run_id=truth.shocked.run_id,
        prediction_id=prediction.prediction_id,
        shock_id=truth.shock.shock_id,
        name=name or f"prediction:{prediction.predictor}",
        predictor=str(prediction.predictor),
        model_version=prediction.model_version,
        dataset_version=dataset_version,
        horizon_hours=truth.horizon_hours,
        classification=classification,
        timing=timing,
        by_horizon=by_horizon,
        seed=seed,
        config_hash=prediction.config_hash,
        metadata={"ground_truth": truth.summary()},
    )
    logger.info("prediction_evaluated", **result.headline())
    return result


def evaluate_search(
    result: SearchResult,
    truth: GroundTruth,
    *,
    optimal_disruption: float | None = None,
    optimal_cost: float | None = None,
    name: str = "",
    dataset_version: str | None = None,
    seed: int | None = None,
) -> EvaluationResult:
    """Score an intervention search, including its gap to the optimum."""
    metrics = intervention_metrics(
        baseline_disruption=result.baseline_disruption,
        achieved_disruption=result.achieved_disruption,
        cost=result.cost,
        n_actions=len(result.plan.interventions),
        optimal_disruption=optimal_disruption,
        optimal_cost=optimal_cost,
        search_ms=result.elapsed_ms,
        candidates_considered=result.candidates_considered,
        simulations_run=result.simulations_run,
    )
    evaluation = EvaluationResult(
        run_id=truth.shocked.run_id,
        plan_id=result.plan.plan_id,
        shock_id=truth.shock.shock_id,
        name=name or f"search:{result.optimizer}",
        optimizer=str(result.optimizer),
        dataset_version=dataset_version,
        horizon_hours=truth.horizon_hours,
        intervention=metrics,
        seed=seed,
        metadata={
            "search": result.summary(),
            "ground_truth": truth.summary(),
            # The objective includes the network's standing disruption, which no
            # intervention on this shock can address. Reporting the prevented
            # amount against the *attributable* total is what makes the number
            # interpretable.
            "attributable_disruption": truth.attributable_disruption,
            "share_of_attributable_prevented": (
                result.disruption_prevented / truth.attributable_disruption
                if truth.attributable_disruption > 0
                else None
            ),
        },
    )
    logger.info("search_evaluated", **evaluation.headline())
    return evaluation


@dataclass(slots=True)
class ComparisonReport:
    """Several predictors and optimisers scored on the same scenario."""

    shock_id: str
    ground_truth: dict[str, Any]
    predictions: list[EvaluationResult] = field(default_factory=list)
    searches: list[EvaluationResult] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "shock_id": self.shock_id,
            "ground_truth": self.ground_truth,
            "predictions": [e.headline() for e in self.predictions],
            "searches": [e.headline() for e in self.searches],
            "elapsed_ms": self.elapsed_ms,
        }

    def best_predictor(self) -> EvaluationResult | None:
        scored = [
            (e, e.classification) for e in self.predictions if e.classification is not None
        ]
        return max(scored, key=lambda pair: pair[1].f1)[0] if scored else None

    def best_search(self) -> EvaluationResult | None:
        scored = [
            (e, e.intervention) for e in self.searches if e.intervention is not None
        ]
        if not scored:
            return None
        return max(scored, key=lambda pair: pair[1].disruption_prevented)[0]


def compare_predictors(
    graph: TemporalPaymentGraph,
    shock: Shock,
    predictors: dict[str, Any],
    *,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
    baseline: CascadeResult | None = None,
    dataset_version: str | None = None,
) -> ComparisonReport:
    """Score every predictor in ``predictors`` on the same shock."""
    started = time.perf_counter()
    truth = build_ground_truth(
        graph, shock, config=config, objective=objective, baseline=baseline
    )

    evaluations = []
    for name, predictor in predictors.items():
        prediction = predictor.predict(graph, shock)
        evaluations.append(
            evaluate_prediction(
                prediction,
                truth,
                graph,
                name=name,
                dataset_version=dataset_version,
            )
        )

    return ComparisonReport(
        shock_id=shock.shock_id,
        ground_truth=truth.summary(),
        predictions=evaluations,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


def compare_searches(
    graph: TemporalPaymentGraph,
    shock: Shock,
    candidates: list[Any],
    searches: dict[str, Any],
    search_config: SearchConfig,
    *,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
    baseline: CascadeResult | None = None,
    reference: str | None = "exhaustive",
    dataset_version: str | None = None,
) -> ComparisonReport:
    """Run several search strategies on one candidate set and score them.

    When ``reference`` names one of the strategies, its result is treated as
    :math:`U^*` and every other strategy gets a real optimality gap. That only
    holds if the reference really is exhaustive - naming a heuristic here would
    produce gaps against an arbitrary baseline, so keep it exhaustive or None.
    """
    started = time.perf_counter()
    truth = build_ground_truth(
        graph, shock, config=config, objective=objective, baseline=baseline
    )
    sim_config = config or SimulationConfig()

    results: dict[str, SearchResult] = {}
    for name, search in searches.items():
        evaluator = CounterfactualEvaluator(
            graph=graph, shock=shock, config=sim_config, objective=objective
        )
        results[name] = search.run(evaluator, candidates, search_config)

    optimum = results.get(reference) if reference else None
    evaluations = [
        evaluate_search(
            result,
            truth,
            optimal_disruption=optimum.achieved_disruption if optimum else None,
            optimal_cost=optimum.cost if optimum else None,
            name=name,
            dataset_version=dataset_version,
        )
        for name, result in results.items()
    ]

    return ComparisonReport(
        shock_id=shock.shock_id,
        ground_truth=truth.summary(),
        searches=evaluations,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )
