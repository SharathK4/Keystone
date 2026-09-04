"""The end-to-end experiment pipeline.

One call executes the full claim of the system and measures it:

1. Generate a network from the config (ground truth is retained but withheld).
2. Learn the dependency structure from the *historical* event stream only.
3. Score the learner against the generator's true parameters.
4. Simulate the no-shock baseline once.
5. For each sampled shock: simulate the truth, run every predictor, score them,
   build a candidate set, run every optimiser, and score those against an
   exhaustive optimum where one is affordable.

Everything is driven from a single :class:`~lce.experiments.config.ExperimentConfig`,
and every step is wrapped in a tracked run, so the report can be reproduced from
its manifest alone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lce.data.generator import NetworkGenerator, SyntheticNetwork
from lce.domain.enums import PredictorKind, RunKind
from lce.domain.evaluation import EvaluationResult
from lce.domain.shock import Shock
from lce.errors import OptimizationError
from lce.evaluation.harness import build_ground_truth, evaluate_prediction, evaluate_search
from lce.experiments.config import ExperimentConfig
from lce.experiments.tracker import RunTracker
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.models.dependency import DependencyLearner, compare_to_ground_truth
from lce.models.propagation import HawkesCascadePredictor, LinearThresholdPropagator
from lce.optimization.candidates import generate_candidates
from lce.optimization.search import SearchConfig, build_search
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.scenarios import unit_shock

logger = get_logger(__name__)


@dataclass(slots=True)
class ExperimentReport:
    """Everything an experiment measured."""

    experiment_id: str
    config: ExperimentConfig
    dataset_version: str
    seeds: dict[str, int]
    dependency_metrics: dict[str, float] = field(default_factory=dict)
    prediction_evaluations: list[EvaluationResult] = field(default_factory=list)
    search_evaluations: list[EvaluationResult] = field(default_factory=list)
    network_summary: dict[str, Any] = field(default_factory=dict)
    shocks: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def predictor_summary(self) -> dict[str, dict[str, float]]:
        """Mean precision/recall/F1/PR-AUC per predictor across all shocks."""
        return _aggregate(
            self.prediction_evaluations,
            key=lambda e: e.predictor or "unknown",
            extract=lambda e: {
                "precision": e.classification.precision,
                "recall": e.classification.recall,
                "f1": e.classification.f1,
                "pr_auc": e.classification.pr_auc,
                "hit_time_mae_hours": e.timing.mae_hours if e.timing else None,
            }
            if e.classification
            else {},
        )

    def optimizer_summary(self) -> dict[str, dict[str, float]]:
        """Mean prevented / cost / DPR / optimality gap per optimiser."""
        return _aggregate(
            self.search_evaluations,
            key=lambda e: e.optimizer or "unknown",
            extract=lambda e: {
                "disruption_prevented": e.intervention.disruption_prevented,
                "cost": e.intervention.cost,
                "dpr": _finite(e.intervention.disruption_prevented_per_rupee),
                "optimality_gap": e.intervention.optimality_gap,
                "simulations_run": float(e.intervention.simulations_run),
            }
            if e.intervention
            else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "name": self.config.name,
            "config_hash": self.config.config_hash,
            "dataset_version": self.dataset_version,
            "seeds": self.seeds,
            "network": self.network_summary,
            "n_shocks": len(self.shocks),
            "dependency_recovery": self.dependency_metrics,
            "predictors": self.predictor_summary(),
            "optimizers": self.optimizer_summary(),
            "elapsed_ms": self.elapsed_ms,
            "warnings": self.warnings,
        }


def _finite(value: float | None) -> float | None:
    """Drop infinities so aggregates stay meaningful (free plans report inf DPR)."""
    if value is None:
        return None
    return value if np.isfinite(value) else None


def _aggregate(
    evaluations: list[EvaluationResult],
    key: Any,
    extract: Any,
) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for evaluation in evaluations:
        payload = extract(evaluation)
        if payload:
            buckets.setdefault(key(evaluation), []).append(payload)

    summary: dict[str, dict[str, float]] = {}
    for name, rows in buckets.items():
        merged: dict[str, float] = {"n": float(len(rows))}
        for field_name in rows[0]:
            values = [r[field_name] for r in rows if r.get(field_name) is not None]
            if values:
                merged[field_name] = float(np.mean(values))
        summary[name] = merged
    return summary


class ExperimentRunner:
    """Executes an :class:`ExperimentConfig` end to end."""

    def __init__(
        self,
        config: ExperimentConfig,
        *,
        tracker: RunTracker | None = None,
    ) -> None:
        self.config = config
        self.tracker = tracker or RunTracker(persist_db=False)
        self.seeds = config.seed_bundle()

    # ------------------------------------------------------------------- run

    def run(self) -> ExperimentReport:
        started = time.perf_counter()
        cfg = self.config
        experiment_id = f"exp_{cfg.config_hash}"
        report = ExperimentReport(
            experiment_id=experiment_id,
            config=cfg,
            dataset_version=cfg.dataset_version,
            seeds=self.seeds.to_dict(),
        )

        network = self._generate(experiment_id)
        graph = network.graph
        report.network_summary = network.summary()

        self._install_dependencies(graph, network, report, experiment_id)

        baseline = None
        shocks = self._sample_shocks(graph, network)
        report.shocks = [s.shock_id for s in shocks]

        for shock in shocks:
            truth = build_ground_truth(
                graph,
                shock,
                config=cfg.simulation,
                objective=cfg.objective,
                baseline=baseline,
            )
            baseline = truth.baseline  # shock-independent; compute once

            predictions = self._run_predictors(graph, shock, report, experiment_id)
            for name, prediction in predictions.items():
                report.prediction_evaluations.append(
                    evaluate_prediction(
                        prediction,
                        truth,
                        graph,
                        name=name,
                        dataset_version=cfg.dataset_version,
                        seed=cfg.seed,
                    )
                )

            self._run_optimizers(graph, shock, predictions, truth, report, experiment_id)

        report.elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info("experiment_complete", **report.to_dict())
        return report

    # ----------------------------------------------------------------- steps

    def _generate(self, experiment_id: str) -> SyntheticNetwork:
        cfg = self.config
        with self.tracker.run(
            RunKind.GENERATION,
            name=f"{cfg.name}:generate",
            experiment_id=experiment_id,
            dataset_version=cfg.dataset_version,
            seed=cfg.seed,
            config=cfg.generator.to_dict(),
            config_hash=cfg.config_hash,
            seeds=self.seeds,
        ) as record:
            network = NetworkGenerator(cfg.generator).generate()
            record.metrics = {"n_merchants": len(network.graph), **network.stats}
            return network

    def _install_dependencies(
        self,
        graph: TemporalPaymentGraph,
        network: SyntheticNetwork,
        report: ExperimentReport,
        experiment_id: str,
    ) -> None:
        """Install either the learned overlay or (for ablation) the true one."""
        cfg = self.config
        if cfg.use_ground_truth_edges:
            report.warnings.append(
                "using ground-truth dependency edges: predictor scores are an "
                "upper bound, not an achievable result"
            )
            return  # the generator already installed the true edges

        with self.tracker.run(
            RunKind.TRAINING,
            name=f"{cfg.name}:learn_dependencies",
            experiment_id=experiment_id,
            dataset_version=cfg.dataset_version,
            model_version="marked_hawkes_em",
            seed=cfg.seed,
            config=cfg.learner.to_dict(),
            config_hash=cfg.config_hash,
            seeds=self.seeds,
        ) as record:
            learned = DependencyLearner(cfg.learner).fit_graph(graph, t_end=0.0)
            metrics = compare_to_ground_truth(learned, network.ground_truth_edges)
            graph.clear_dependencies()
            graph.set_dependencies(learned)
            report.dependency_metrics = metrics
            record.metrics = metrics

    def _sample_shocks(
        self, graph: TemporalPaymentGraph, network: SyntheticNetwork
    ) -> list[Shock]:
        """Shock the most structurally significant nodes first.

        Sampling uniformly would spend most of the budget on leaf suppliers with
        no downstream, where every predictor trivially scores zero and the
        comparison carries no information.
        """
        cfg = self.config
        candidates = [
            m
            for m in sorted(graph.merchant_ids)
            if graph.out_dependencies(m) and network.layers.get(m, 99) < cfg.generator.n_layers - 1
        ]
        if not candidates:
            candidates = sorted(graph.merchant_ids)

        ranked = sorted(
            candidates,
            key=lambda m: (-len(graph.descendants_within(m, 3)), m),
        )
        chosen = ranked[: cfg.n_shocks]
        return [
            unit_shock(graph, m, fraction_of_buffer=cfg.shock_fraction_of_buffer)
            for m in chosen
        ]

    def _run_predictors(
        self,
        graph: TemporalPaymentGraph,
        shock: Shock,
        report: ExperimentReport,
        experiment_id: str,
    ) -> dict[str, Any]:
        cfg = self.config
        predictors: dict[str, Any] = {}
        for kind in cfg.predictors:
            if kind is PredictorKind.LINEAR_THRESHOLD:
                predictors["linear_threshold"] = LinearThresholdPropagator(cfg.propagation)
            elif kind is PredictorKind.HAWKES_CASCADE:
                predictors["hawkes_cascade"] = HawkesCascadePredictor(cfg.propagation)
            elif kind is PredictorKind.TEMPORAL_GNN:
                report.warnings.append(
                    "temporal_gnn requested but the runner does not train it inline; "
                    "train it via lce.models.tgnn and evaluate separately"
                )

        out: dict[str, Any] = {}
        for name, predictor in predictors.items():
            with self.tracker.run(
                RunKind.PREDICTION,
                name=f"{cfg.name}:predict:{name}",
                experiment_id=experiment_id,
                dataset_version=cfg.dataset_version,
                model_version=name,
                shock_id=shock.shock_id,
                seed=cfg.seed,
                config=cfg.propagation.to_dict(),
                config_hash=cfg.config_hash,
                seeds=self.seeds,
            ) as record:
                prediction = predictor.predict(graph, shock)
                out[name] = prediction
                record.metrics = {"n_flagged": len(prediction.predicted_affected_ids)}
        return out

    def _run_optimizers(
        self,
        graph: TemporalPaymentGraph,
        shock: Shock,
        predictions: dict[str, Any],
        truth: Any,
        report: ExperimentReport,
        experiment_id: str,
    ) -> None:
        cfg = self.config
        if not predictions:
            return
        # Candidates are built from the strongest available predictor; the
        # optimiser is only ever as good as the exposure ranking it is handed.
        prediction = predictions.get("linear_threshold") or next(iter(predictions.values()))
        candidate_set = generate_candidates(
            graph,
            shock,
            prediction,
            cfg.candidates,
            horizon_hours=cfg.simulation.horizon_hours,
        )
        if not candidate_set.interventions:
            report.warnings.append(f"no candidates generated for shock {shock.shock_id}")
            return

        search_config = SearchConfig(
            budget=cfg.search.budget,
            max_actions=cfg.search.max_actions,
            lazy=cfg.search.lazy,
            min_gain=cfg.search.min_gain,
            one_per_merchant=cfg.search.one_per_merchant,
            cp_sat_time_limit_s=cfg.search.cp_sat_time_limit_s,
        )

        # The reference optimum first, so the others can be scored against it.
        optimal_disruption = optimal_cost = None
        if cfg.reference_optimizer is not None:
            try:
                reference = build_search(cfg.reference_optimizer).run(
                    CounterfactualEvaluator(
                        graph=graph,
                        shock=shock,
                        config=cfg.simulation,
                        objective=cfg.objective,
                    ),
                    candidate_set.interventions,
                    search_config,
                )
                optimal_disruption = reference.achieved_disruption
                optimal_cost = reference.cost
            except OptimizationError as exc:
                report.warnings.append(
                    f"reference optimum unavailable for {shock.shock_id}: {exc.message}; "
                    "optimality gaps will be omitted"
                )

        for kind in cfg.optimizers:
            with self.tracker.run(
                RunKind.OPTIMIZATION,
                name=f"{cfg.name}:optimize:{kind}",
                experiment_id=experiment_id,
                dataset_version=cfg.dataset_version,
                shock_id=shock.shock_id,
                seed=cfg.seed,
                config=cfg.search.to_dict() | {"optimizer": str(kind)},
                config_hash=cfg.config_hash,
                seeds=self.seeds,
            ) as record:
                evaluator = CounterfactualEvaluator(
                    graph=graph,
                    shock=shock,
                    config=cfg.simulation,
                    objective=cfg.objective,
                )
                result = build_search(kind).run(
                    evaluator, candidate_set.interventions, search_config
                )
                evaluation = evaluate_search(
                    result,
                    truth,
                    optimal_disruption=optimal_disruption,
                    optimal_cost=optimal_cost,
                    name=str(kind),
                    dataset_version=cfg.dataset_version,
                    seed=cfg.seed,
                )
                report.search_evaluations.append(evaluation)
                record.plan_id = result.plan.plan_id
                record.metrics = result.summary()


def run_experiment(config: ExperimentConfig) -> ExperimentReport:
    """Convenience entry point."""
    return ExperimentRunner(config).run()
