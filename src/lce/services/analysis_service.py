"""Simulation, prediction and intervention orchestration.

This is what the API routes call. Each method opens a tracked run, does the
work, persists the result, and returns domain objects - so a request made
through HTTP leaves exactly the same provenance trail as one made from a script.
"""

from __future__ import annotations

import time
from typing import Any

from lce.config import ObjectiveSettings, get_settings
from lce.data.unit_of_work import UnitOfWork
from lce.domain.enums import OptimizerKind, PredictorKind, RunKind
from lce.domain.evaluation import EvaluationResult
from lce.domain.intervention import InterventionPlan
from lce.domain.prediction import ModelPrediction
from lce.domain.propagation import CascadeResult
from lce.domain.shock import Shock
from lce.errors import NotFoundError, ValidationError
from lce.evaluation.harness import build_ground_truth, evaluate_prediction, evaluate_search
from lce.experiments.tracker import RunTracker
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.models.dependency import DependencyLearner, DependencyLearnerConfig
from lce.models.propagation import (
    HawkesCascadePredictor,
    LinearThresholdPropagator,
    PropagationConfig,
)
from lce.optimization.candidates import CandidateConfig, generate_candidates
from lce.optimization.search import SearchConfig, SearchResult, build_search
from lce.optimization.systemic import SystemicRanking, compute_systemic_importance
from lce.services.network_service import NetworkService
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import LiquiditySimulator, SimulationConfig

logger = get_logger(__name__)

_PREDICTORS = {
    PredictorKind.LINEAR_THRESHOLD: LinearThresholdPropagator,
    PredictorKind.HAWKES_CASCADE: HawkesCascadePredictor,
}


class AnalysisService:
    """Runs simulations, predictions and intervention searches."""

    def __init__(
        self,
        uow: UnitOfWork,
        *,
        tracker: RunTracker | None = None,
        objective: ObjectiveSettings | None = None,
    ) -> None:
        self.uow = uow
        self.tracker = tracker or RunTracker(uow=uow, persist_db=True)
        self.networks = NetworkService(uow, self.tracker)
        self.objective = objective or get_settings().objective

    # ------------------------------------------------------------ dependencies

    def learn_dependencies(
        self,
        dataset_id: str,
        config: DependencyLearnerConfig | None = None,
        *,
        t_end: float = 0.0,
    ) -> dict[str, Any]:
        """Fit the dependency overlay and store it alongside the ground truth."""
        cfg = config or DependencyLearnerConfig()
        graph = self.networks.load_graph(dataset_id, use_cache=False)

        with self.tracker.run(
            RunKind.TRAINING,
            name="learn_dependencies",
            dataset_version=dataset_id,
            model_version="marked_hawkes_em",
            config=cfg.to_dict(),
        ) as record:
            started = time.perf_counter()
            edges = DependencyLearner(cfg).fit_graph(graph, t_end=t_end)
            estimator = edges[0].estimator if edges else "marked_hawkes_em"
            self.uow.edges.replace_for_estimator(
                edges, dataset_id, estimator or "marked_hawkes_em"
            )
            self.uow.commit()
            NetworkService.invalidate_cache()

            metrics = {
                "n_edges": len(edges),
                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                "mean_pass_through": (
                    sum(e.pass_through for e in edges) / len(edges) if edges else 0.0
                ),
                "mean_confidence": (
                    sum(e.confidence for e in edges) / len(edges) if edges else 0.0
                ),
            }
            record.metrics = metrics
            return {"dataset_id": dataset_id, "estimator": estimator, **metrics}

    # ---------------------------------------------------------------- shocks

    def save_shock(self, shock: Shock, dataset_id: str) -> Shock:
        self.uow.shocks.save(shock, dataset_id)
        self.uow.commit()
        return shock

    def get_shock(self, shock_id: str) -> Shock:
        return self.uow.shocks.require(shock_id)

    # ------------------------------------------------------------- simulation

    def simulate(
        self,
        dataset_id: str,
        shock: Shock | None,
        *,
        plan: InterventionPlan | None = None,
        config: SimulationConfig | None = None,
        estimator: str | None = None,
        store_events: bool = True,
    ) -> CascadeResult:
        """Run one cascade and persist its outcomes (and optionally its events)."""
        graph = self.networks.load_graph(dataset_id, estimator=estimator)
        sim_config = config or SimulationConfig.from_settings()

        with self.tracker.run(
            RunKind.SIMULATION,
            name="simulate",
            dataset_version=dataset_id,
            shock_id=shock.shock_id if shock else None,
            plan_id=plan.plan_id if plan else None,
            seed=sim_config.seed,
            config=sim_config.to_dict(),
        ) as record:
            result = LiquiditySimulator(graph, sim_config, self.objective).run(
                shock, plan, run_id=record.run_id
            )
            self.uow.cascades.save_result(result, store_events=store_events)
            self.uow.commit()
            record.metrics = result.summary()
            return result

    def get_cascade(self, run_id: str) -> dict[str, Any]:
        run = self.uow.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"unknown run {run_id!r}", run_id=run_id)
        outcomes = self.uow.cascades.outcomes_for_run(run_id)
        if not outcomes:
            raise NotFoundError(f"run {run_id!r} has no stored cascade outcomes")
        return {
            "run_id": run_id,
            "status": run.status,
            "shock_id": run.shock_id,
            "plan_id": run.plan_id,
            "metrics": run.metrics,
            "affected": self.uow.cascades.affected_ids(run_id),
            "outcomes": {m: o.model_dump(mode="json") for m, o in outcomes.items()},
        }

    # ------------------------------------------------------------- prediction

    def predict(
        self,
        dataset_id: str,
        shock: Shock,
        *,
        predictor: PredictorKind = PredictorKind.LINEAR_THRESHOLD,
        config: PropagationConfig | None = None,
        estimator: str | None = None,
    ) -> ModelPrediction:
        """Predict contagion for a shock and persist the prediction."""
        factory = _PREDICTORS.get(predictor)
        if factory is None:
            raise ValidationError(
                f"predictor {predictor!r} is not available through this endpoint; "
                "the temporal GNN must be trained and loaded explicitly",
                predictor=str(predictor),
            )

        graph = self.networks.load_graph(dataset_id, estimator=estimator)
        cfg = config or PropagationConfig()

        with self.tracker.run(
            RunKind.PREDICTION,
            name=f"predict:{predictor}",
            dataset_version=dataset_id,
            model_version=str(predictor),
            shock_id=shock.shock_id,
            config=cfg.to_dict(),
        ) as record:
            started = time.perf_counter()
            prediction = factory(cfg).predict(graph, shock, run_id=record.run_id)
            prediction = prediction.model_copy(
                update={"inference_ms": (time.perf_counter() - started) * 1000.0}
            )
            self.uow.predictions.save(prediction, dataset_version=dataset_id)
            self.uow.commit()
            record.metrics = {
                "n_flagged": len(prediction.predicted_affected_ids),
                "inference_ms": prediction.inference_ms,
            }
            return prediction

    # ----------------------------------------------------------- intervention

    def optimize(
        self,
        dataset_id: str,
        shock: Shock,
        *,
        optimizer: OptimizerKind = OptimizerKind.GREEDY,
        search_config: SearchConfig | None = None,
        candidate_config: CandidateConfig | None = None,
        simulation_config: SimulationConfig | None = None,
        prediction: ModelPrediction | None = None,
        estimator: str | None = None,
    ) -> SearchResult:
        """Search for the cheapest intervention that most reduces disruption."""
        graph = self.networks.load_graph(dataset_id, estimator=estimator)
        sim_config = simulation_config or SimulationConfig.from_settings()
        s_config = search_config or SearchConfig()
        c_config = candidate_config or CandidateConfig()

        if prediction is None:
            prediction = LinearThresholdPropagator(
                PropagationConfig(horizon_hours=sim_config.horizon_hours)
            ).predict(graph, shock)

        candidates = generate_candidates(
            graph, shock, prediction, c_config, horizon_hours=sim_config.horizon_hours
        )
        if not candidates.interventions:
            raise ValidationError(
                "no intervention candidates could be generated for this shock; "
                "the predictor flagged no exposed nodes with actionable obligations",
                shock_id=shock.shock_id,
            )

        with self.tracker.run(
            RunKind.OPTIMIZATION,
            name=f"optimize:{optimizer}",
            dataset_version=dataset_id,
            shock_id=shock.shock_id,
            seed=sim_config.seed,
            config=s_config.to_dict() | {"optimizer": str(optimizer)},
        ) as record:
            evaluator = CounterfactualEvaluator(
                graph=graph, shock=shock, config=sim_config, objective=self.objective
            )
            result = build_search(optimizer).run(evaluator, candidates.interventions, s_config)
            self.uow.plans.save(
                result.plan, dataset_id, shock_id=shock.shock_id, run_id=record.run_id
            )
            self.uow.commit()
            record.plan_id = result.plan.plan_id
            record.metrics = result.summary()
            return result

    # -------------------------------------------------------------- analysis

    def evaluate(
        self,
        dataset_id: str,
        shock: Shock,
        *,
        prediction: ModelPrediction,
        simulation_config: SimulationConfig | None = None,
        estimator: str | None = None,
    ) -> EvaluationResult:
        """Score a stored prediction against a fresh simulation of the truth."""
        graph = self.networks.load_graph(dataset_id, estimator=estimator)
        sim_config = simulation_config or SimulationConfig.from_settings()

        with self.tracker.run(
            RunKind.EVALUATION,
            name="evaluate_prediction",
            dataset_version=dataset_id,
            shock_id=shock.shock_id,
            seed=sim_config.seed,
            config=sim_config.to_dict(),
        ) as record:
            truth = build_ground_truth(
                graph, shock, config=sim_config, objective=self.objective
            )
            evaluation = evaluate_prediction(
                prediction, truth, graph, dataset_version=dataset_id, seed=sim_config.seed
            )
            self.uow.evaluations.save(evaluation)
            self.uow.commit()
            record.metrics = evaluation.headline()
            return evaluation

    def evaluate_plan(
        self,
        dataset_id: str,
        shock: Shock,
        result: SearchResult,
        *,
        optimal_disruption: float | None = None,
        simulation_config: SimulationConfig | None = None,
        estimator: str | None = None,
    ) -> EvaluationResult:
        graph = self.networks.load_graph(dataset_id, estimator=estimator)
        sim_config = simulation_config or SimulationConfig.from_settings()
        truth = build_ground_truth(
            graph, shock, config=sim_config, objective=self.objective
        )
        evaluation = evaluate_search(
            result,
            truth,
            optimal_disruption=optimal_disruption,
            dataset_version=dataset_id,
            seed=sim_config.seed,
        )
        self.uow.evaluations.save(evaluation)
        self.uow.commit()
        return evaluation

    def systemic_importance(
        self,
        dataset_id: str,
        *,
        simulation_config: SimulationConfig | None = None,
        shock_fraction: float = 1.5,
        limit: int | None = None,
        estimator: str | None = None,
    ) -> SystemicRanking:
        """Rank merchants by the damage a standardised shock at each one causes."""
        graph = self.networks.load_graph(dataset_id, estimator=estimator)
        sim_config = simulation_config or SimulationConfig.from_settings()
        merchants = sorted(graph.merchant_ids)[:limit] if limit else None

        with self.tracker.run(
            RunKind.EVALUATION,
            name="systemic_importance",
            dataset_version=dataset_id,
            seed=sim_config.seed,
            config={"shock_fraction": shock_fraction, **sim_config.to_dict()},
        ) as record:
            ranking = compute_systemic_importance(
                graph,
                config=sim_config,
                objective=self.objective,
                shock_fraction=shock_fraction,
                merchants=merchants,
            )
            top = ranking.ranked(5)
            record.metrics = {"n_ranked": len(ranking.simulated), "top": dict(top)}
            return ranking
