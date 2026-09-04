"""The Phase-4 pipeline: predict, decide, replay, measure, record.

One entry point per scenario, and the order is the claim:

1. **predict** the cascade from observable pre-shock information only;
2. **generate** feasible candidate actions and rank them explainably;
3. **solve** over the true simulator - greedy, the pairwise MILP, and on small
   networks a complete enumeration;
4. **replay** every strategy's choice in the simulator;
5. **compare** against no intervention and four named naive rules;
6. **record** everything, with enough provenance to reproduce the decision.

The prediction never scores itself. It decides what to consider; the simulator
decides what happened, and only the simulator's number is reported.

Two prediction sources
----------------------
``propagation``
    marked-Hawkes structure estimated from the observable pre-origin stream, then
    the analytic threshold propagator. Needs no trained artifact, so a Phase-4
    run works from a clean checkout.

``artifact``
    an exported Phase-3 bundle served through :mod:`lce.inference`. The same code
    path production uses, which is the point of exercising it here.

Both respect the Phase-3 observability barrier: features and structure come from
events strictly before the shock onset.
"""

from __future__ import annotations

import json
import platform
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lce import __version__
from lce.benchmark.scales import BenchmarkScale, scale_config
from lce.benchmark.scenarios import (
    BuiltScenario,
    ScenarioFamily,
    baseline_affected_set,
    scenario_suite,
)
from lce.config import ObjectiveSettings
from lce.data.generator import SyntheticNetwork, generate_network
from lce.domain.intervention import Intervention
from lce.domain.prediction import ModelPrediction
from lce.errors import OptimizationError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.intervention.actions import (
    ActionSet,
    baseline_actions,
    generate_actions,
    rank_by_cash_cover,
    rank_by_degree,
    rank_by_open_deficit,
)
from lce.intervention.evaluate import (
    CounterfactualReport,
    build_outcome,
    replay,
    score_against_reference,
    summarise,
)
from lce.intervention.exact import solve_exact, solve_milp
from lce.intervention.problem import ObjectiveSpec
from lce.intervention.profiles import ResourceBudget, ResourceProfile, budget_for
from lce.intervention.robust import UncertaintySpec, plans_from_solver, robust_select
from lce.intervention.scalable import benchmark_pruning, greedy_solve
from lce.logging import get_logger
from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
from lce.optimization.systemic import SystemicRanking, compute_systemic_importance
from lce.seeds import config_hash
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import SimulationConfig

logger = get_logger(__name__)

PREDICTORS: tuple[str, ...] = ("propagation", "artifact")


@dataclass(frozen=True, slots=True)
class Phase4Config:
    """Everything that determines a Phase-4 result."""

    profile: ResourceProfile = ResourceProfile.SMALL_FAST
    seeds: tuple[int, ...] = (2025, 7, 99)
    magnitude: float = 2.0
    families: tuple[ScenarioFamily, ...] | None = None
    objective: ObjectiveSpec = field(default_factory=ObjectiveSpec)
    uncertainty: UncertaintySpec = field(default_factory=UncertaintySpec)
    robust: bool = True
    predictor: str = "propagation"
    artifact_root: str | None = None
    artifact_version: str | None = None
    systemic: bool = True
    pruning_benchmark: bool = True
    seed: int = 20250101

    def __post_init__(self) -> None:
        if self.predictor not in PREDICTORS:
            raise ValueError(f"unknown predictor {self.predictor!r}; expected {PREDICTORS}")

    @property
    def budget(self) -> ResourceBudget:
        return budget_for(self.profile)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": str(self.profile),
            "seeds": list(self.seeds),
            "magnitude": self.magnitude,
            "families": [str(f) for f in self.families] if self.families else None,
            "objective": self.objective.to_dict(),
            "uncertainty": self.uncertainty.to_dict(),
            "robust": self.robust,
            "predictor": self.predictor,
            "artifact_version": self.artifact_version,
            "systemic": self.systemic,
            "pruning_benchmark": self.pruning_benchmark,
            "seed": self.seed,
            "resource_budget": self.budget.to_dict(),
        }

    @property
    def config_hash(self) -> str:
        return config_hash(self.to_dict())


# --------------------------------------------------------------- prediction


def predict_with_propagation(
    scenario: BuiltScenario, *, sim_config: SimulationConfig
) -> tuple[ModelPrediction, TemporalPaymentGraph, dict[str, Any]]:
    """Estimate structure from observable history, then propagate analytically.

    The overlay is fitted on events strictly before the shock onset - the
    Phase-3 barrier, unchanged - and installed on the *unperturbed* graph, so
    nothing the shock did to the obligation book is visible to the predictor.
    """
    from lce.learning.pointprocess import HawkesDependencyEstimator
    from lce.learning.problem import baseline_payment_stream, build_observed_window

    started = time.perf_counter()
    source = scenario.unperturbed_graph
    stream = baseline_payment_stream(source, sim_config)
    window = build_observed_window(scenario, config=sim_config, baseline_payments=stream)

    estimator = HawkesDependencyEstimator()
    graph = estimator.install(window)
    prediction = LinearThresholdPropagator(
        PropagationConfig(horizon_hours=sim_config.horizon_hours)
    ).predict(graph, scenario.shock)

    meta = {
        "source": "propagation",
        "n_learned_edges": len(graph.dependency_edges),
        "n_observed_events": window.graph.stats().n_payment_events,
        "origin_t": window.origin_t,
        "calibrated": False,
        "note": (
            "exposure scores come from the analytic propagator and are not "
            "calibrated probabilities; they are used only to rank candidates"
        ),
        "runtime_s": round(time.perf_counter() - started, 4),
    }
    return prediction, graph, meta


def predict_with_artifact(
    scenario: BuiltScenario,
    *,
    sim_config: SimulationConfig,
    artifact_root: Path | None,
    version: str | None,
) -> tuple[ModelPrediction, TemporalPaymentGraph, dict[str, Any]]:
    """Serve the prediction through the production inference path.

    Deliberately the same call the API makes. A research result produced by a
    different code path than the deployed one is a result about the research
    code, not about what ships.
    """
    from lce.inference.predictor import NetworkState
    from lce.inference.service import InferenceService, prediction_to_model_prediction
    from lce.learning.pointprocess import HawkesDependencyEstimator
    from lce.learning.problem import baseline_payment_stream, build_observed_window

    started = time.perf_counter()
    source = scenario.unperturbed_graph
    stream = baseline_payment_stream(source, sim_config)
    window = build_observed_window(scenario, config=sim_config, baseline_payments=stream)

    service = InferenceService(artifact_root, version=version)
    state = NetworkState(
        network_id=scenario.dataset_id,
        merchants=list(window.graph.merchants.values()),
        obligations=window.graph.obligations,
        payments=window.graph.payment_events,
    )
    served, _ = service.predict_contagion(
        state,
        scenario.shock,
        observation_cutoff=window.origin_t,
        horizon_hours=sim_config.horizon_hours,
    )

    # The optimiser still needs a structural overlay to size term-structure
    # actions against; it is estimated the same way, from the same window.
    graph = HawkesDependencyEstimator().install(window)
    meta = {
        "source": "artifact",
        "model_version": served.model_version,
        "artifact_hash": served.artifact_hash,
        "feature_schema_version": served.feature_schema_version,
        "calibrator": served.calibrator,
        "calibrated": True,
        "n_flagged": len(served.flagged()),
        "origin_t": window.origin_t,
        "runtime_s": round(time.perf_counter() - started, 4),
    }
    return (
        prediction_to_model_prediction(served, horizon_hours=sim_config.horizon_hours),
        graph,
        meta,
    )


# ------------------------------------------------------------------ one scenario


@dataclass(slots=True)
class ScenarioResult:
    """Everything one scenario produced."""

    scenario_id: str
    dataset_id: str
    family: str
    prediction: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, Any] = field(default_factory=dict)
    counterfactual: CounterfactualReport = field(default_factory=CounterfactualReport)
    robustness: dict[str, Any] = field(default_factory=dict)
    pruning: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "dataset_id": self.dataset_id,
            "family": self.family,
            "prediction": self.prediction,
            "candidates": self.candidates,
            "counterfactual": self.counterfactual.to_dict(),
            "robustness": self.robustness,
            "pruning": self.pruning,
            "timing": self.timing,
        }


def run_scenario(
    scenario: BuiltScenario,
    *,
    config: Phase4Config,
    sim_config: SimulationConfig,
    systemic: SystemicRanking | None = None,
    objective_settings: ObjectiveSettings | None = None,
    measure_pruning: bool = True,
) -> ScenarioResult:
    """Predict, decide, replay and compare on one scenario."""
    budget = config.budget
    started = time.perf_counter()
    timing: dict[str, float] = {}

    # --- 1. predict, from observables only ---------------------------------
    mark = time.perf_counter()
    if config.predictor == "artifact":
        prediction, learned_graph, meta = predict_with_artifact(
            scenario,
            sim_config=sim_config,
            artifact_root=Path(config.artifact_root) if config.artifact_root else None,
            version=config.artifact_version,
        )
    else:
        prediction, learned_graph, meta = predict_with_propagation(
            scenario, sim_config=sim_config
        )
    timing["predict_s"] = time.perf_counter() - mark

    # The decision is taken at the shock onset: that is when the operator learns
    # anything is wrong, so no action may be scheduled before it.
    onset = scenario.shock.onset_t
    constraints = replace(
        budget.constraints(),
        decision_time=onset,
        horizon_hours=sim_config.horizon_hours,
    )

    # --- 2. candidates, feasibility first ----------------------------------
    mark = time.perf_counter()
    action_set: ActionSet = generate_actions(
        learned_graph,
        scenario.shock,
        prediction,
        constraints=constraints,
        max_candidates=budget.max_candidates,
    )
    timing["candidates_s"] = time.perf_counter() - mark

    graph = scenario.graph
    no_intervention = replay(
        graph, scenario.shock, [], config=sim_config,
        objective_settings=objective_settings, run_id="replay:none",
    )

    report = CounterfactualReport(
        scenario_id=scenario.scenario_id,
        dataset_id=scenario.dataset_id,
        family=str(scenario.spec.family),
        objective=config.objective,
    )

    def add(name: str, actions: Sequence[Intervention], **kw: Any) -> None:
        report.outcomes.append(
            build_outcome(
                name,
                actions,
                graph=graph,
                shock=scenario.shock,
                config=sim_config,
                constraints=constraints,
                no_intervention=no_intervention,
                objective_settings=objective_settings,
                **kw,
            )
        )

    add("no_intervention", [])

    # --- 3. naive rules ------------------------------------------------------
    mark = time.perf_counter()
    horizon = sim_config.horizon_hours
    rules: list[tuple[str, list[str]]] = [
        ("naive_largest_deficit", rank_by_open_deficit(graph, horizon)),
        ("highest_degree", rank_by_degree(graph)),
        ("cash_cover", rank_by_cash_cover(graph, horizon)),
    ]
    if systemic is not None and systemic.probes:
        rules.append(("highest_systemic_importance", [m for m, _ in systemic.ranked()]))

    baseline_chosen: list[Intervention] = []
    for name, ranking in rules:
        actions = baseline_actions(
            graph, ranking, constraints=constraints, t=onset, rule=name, max_actions=1
        )
        baseline_chosen.extend(actions)
        add(name, actions)
    timing["baselines_s"] = time.perf_counter() - mark

    # --- 4. model-guided, over the true simulator ---------------------------
    mark = time.perf_counter()
    evaluator = CounterfactualEvaluator(
        graph=graph, shock=scenario.shock, config=sim_config, objective=objective_settings
    )
    greedy = greedy_solve(
        evaluator,
        action_set.interventions,
        graph,
        constraints=constraints,
        objective=config.objective,
    )
    add(
        "model_guided_greedy",
        greedy.interventions,
        selection_runtime_s=greedy.runtime_s,
        simulations=greedy.simulations,
        notes={"solver": greedy.status, "trace": greedy.notes.get("trace", [])},
    )
    timing["greedy_s"] = time.perf_counter() - mark

    # --- 5. MILP over the measured pairwise surrogate ------------------------
    if action_set.interventions and budget.pairwise_surrogate:
        mark = time.perf_counter()
        try:
            milp_evaluator = CounterfactualEvaluator(
                graph=graph, shock=scenario.shock, config=sim_config,
                objective=objective_settings,
            )
            milp = solve_milp(
                milp_evaluator,
                action_set.interventions,
                graph,
                constraints=constraints,
                objective=config.objective,
                time_limit_s=budget.solver_time_limit_s,
                pairwise=True,
            )
            add(
                "model_guided_milp",
                milp.interventions,
                predicted_disruption=milp.notes.get("surrogate_predicted_disruption"),
                selection_runtime_s=milp.runtime_s,
                simulations=milp.simulations,
                notes={
                    "solver": milp.status,
                    "gap": milp.gap,
                    "surrogate": milp.notes.get("surrogate"),
                    "surrogate_error": milp.notes.get("surrogate_error"),
                },
            )
        except Exception as exc:  # solver extra missing, or infeasible model
            logger.warning("milp_unavailable", error=str(exc))
        timing["milp_s"] = time.perf_counter() - mark

    # --- 6. exact optimum, small networks only -------------------------------
    #
    # Searched over the union of every strategy's actions, not just the model's
    # own candidate set. A reference optimum restricted to the candidates the
    # model proposed is not an optimum at all - a naive rule picking outside that
    # set can beat it, and the resulting "optimality gap" comes back negative,
    # which is a sign the reference was wrong rather than that the heuristic was
    # brilliant.
    exact_candidates = _union(action_set.interventions, baseline_chosen)
    if budget.exact_optimum and exact_candidates:
        mark = time.perf_counter()
        try:
            exact_evaluator = CounterfactualEvaluator(
                graph=graph, shock=scenario.shock, config=sim_config,
                objective=objective_settings,
            )
            exact = solve_exact(
                exact_evaluator,
                exact_candidates,
                graph,
                constraints=constraints,
                objective=config.objective,
            )
            add(
                "exact_optimum",
                exact.interventions,
                selection_runtime_s=exact.runtime_s,
                simulations=exact.simulations,
                notes={
                    "solver": exact.status,
                    "subsets_evaluated": exact.subsets_evaluated,
                    "proof": exact.notes.get("proof"),
                    "search_space": (
                        "union of the model's candidate set and every naive "
                        "rule's action, so the reference dominates all reported "
                        "strategies by construction"
                    ),
                    "n_candidates": len(exact_candidates),
                },
            )
            score_against_reference(report, "exact_optimum", config.objective)
        except OptimizationError as exc:
            logger.warning("exact_optimum_unavailable", error=exc.message)
        timing["exact_s"] = time.perf_counter() - mark

    # --- 6b. what did pruning cost? -----------------------------------------
    #
    # The gap above mixes two different failures: the search may have missed the
    # best subset of what it was given, and the candidate filter may never have
    # given it the right action at all. This separates them by solving exactly on
    # the full feasible pool and again on the retained set.
    pruning: dict[str, Any] = {}
    if (
        config.pruning_benchmark
        and measure_pruning
        and budget.exact_optimum
        and len(action_set.feasible_pool) > len(action_set.interventions)
    ):
        mark = time.perf_counter()
        try:
            pruning = benchmark_pruning(
                graph,
                scenario.shock,
                action_set.feasible_pool,
                action_set.interventions,
                constraints=constraints,
                objective=config.objective,
                sim_config=sim_config,
                objective_settings=objective_settings,
            ).to_dict()
        except OptimizationError as exc:
            pruning = {"note": exc.message}
        timing["pruning_s"] = time.perf_counter() - mark

    # --- 7. robustness -------------------------------------------------------
    robustness: dict[str, Any] = {}
    if config.robust and action_set.interventions:
        mark = time.perf_counter()
        spec = replace(config.uncertainty, n_scenarios=budget.n_robust_scenarios)
        outcome = robust_select(
            graph,
            plans_from_solver(greedy, action_set.interventions, max_singletons=4),
            shock=scenario.shock,
            config=sim_config,
            constraints=constraints,
            objective=config.objective,
            spec=spec,
            objective_settings=objective_settings,
        )
        robustness = outcome.to_dict()
        add(
            "model_guided_robust",
            outcome.chosen.interventions,
            selection_runtime_s=outcome.runtime_s,
            simulations=outcome.simulations,
            notes={"kappa": spec.kappa, "n_worlds": len(outcome.worlds)},
        )
        if report.reference:
            score_against_reference(report, report.reference, config.objective)
        timing["robust_s"] = time.perf_counter() - mark

    timing["total_s"] = time.perf_counter() - started

    result = ScenarioResult(
        scenario_id=scenario.scenario_id,
        dataset_id=scenario.dataset_id,
        family=str(scenario.spec.family),
        prediction=meta
        | {
            "n_flagged": len(prediction.predicted_affected_ids),
            "flagged": prediction.predicted_affected_ids[:20],
            "true_affected": no_intervention.cascade.affected_ids[:20],
            "n_true_affected": no_intervention.n_affected,
        },
        candidates=action_set.to_dict(),
        counterfactual=report,
        robustness=robustness,
        pruning=pruning,
        timing={k: round(v, 4) for k, v in timing.items()},
    )
    logger.info("phase4_scenario_done", **summarise(report))
    return result


# ------------------------------------------------------------------- the run


@dataclass(slots=True)
class Phase4Report:
    """Every scenario, plus provenance."""

    run_id: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    scenarios: list[ScenarioResult] = field(default_factory=list)
    systemic: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config": self.config,
            "provenance": self.provenance,
            "systemic_importance": self.systemic,
            "scenarios": [s.to_dict() for s in self.scenarios],
            "summary": self.summary(),
            "elapsed_s": round(self.elapsed_s, 2),
        }

    def summary(self) -> dict[str, Any]:
        """Mean outcome per strategy across every scenario in the run."""
        by_strategy: dict[str, list[Any]] = {}
        for scenario in self.scenarios:
            for outcome in scenario.counterfactual.outcomes:
                by_strategy.setdefault(outcome.name, []).append(outcome)

        rows = []
        for name, outcomes in sorted(by_strategy.items()):
            reductions = [o.disruption_reduction_pct for o in outcomes]
            costs = [o.cost for o in outcomes]
            gaps = [o.relative_gap for o in outcomes if o.relative_gap is not None]
            regrets = [o.regret for o in outcomes if o.regret is not None]
            efficiencies = [
                o.capital_efficiency
                for o in outcomes
                if o.cost > 0 and o.capital_efficiency == o.capital_efficiency
            ]
            rows.append(
                {
                    "strategy": name,
                    "n": len(outcomes),
                    "mean_reduction_pct": _mean(reductions),
                    "median_reduction_pct": _median(reductions),
                    "mean_cost": _mean(costs),
                    "median_capital_efficiency": _median(efficiencies),
                    "mean_capital_efficiency": _mean(efficiencies),
                    "mean_relative_gap": _mean(gaps),
                    "median_relative_gap": _median(gaps),
                    "mean_regret": _mean(regrets),
                    "n_infeasible": sum(1 for o in outcomes if o.violations),
                    "mean_runtime_s": _mean(
                        [o.selection_runtime_s + o.replay_runtime_s for o in outcomes]
                    ),
                }
            )
        return {"n_scenarios": len(self.scenarios), "strategies": rows}


def _union(*groups: Sequence[Intervention]) -> list[Intervention]:
    """Concatenate action lists, de-duplicated by id and deterministically ordered."""
    seen: dict[str, Intervention] = {}
    for group in groups:
        for action in group:
            seen.setdefault(action.intervention_id, action)
    return [seen[k] for k in sorted(seen)]


def _mean(values: Sequence[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def _median(values: Sequence[float]) -> float | None:
    """Median as well as mean: scenarios differ by orders of magnitude in absolute
    rupees, so a mean over capital efficiency is dominated by whichever draw
    happened to be largest."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return 0.5 * (clean[mid - 1] + clean[mid])


def run_phase4(
    config: Phase4Config = Phase4Config(),
    *,
    objective_settings: ObjectiveSettings | None = None,
    generator_overrides: dict[str, Any] | None = None,
) -> Phase4Report:
    """Run every scenario on every seed under the configured resource profile."""
    started = time.perf_counter()
    budget = config.budget
    report = Phase4Report(
        run_id=f"p4-{config.config_hash}",
        config=config.to_dict(),
        provenance=_provenance(config),
    )

    systemic_payload: dict[str, Any] = {}
    for seed in config.seeds:
        generator = scale_config(
            BenchmarkScale(budget.scale), seed=seed, overrides=generator_overrides
        )
        network: SyntheticNetwork = generate_network(generator)
        sim_config = SimulationConfig(horizon_hours=generator.horizon_hours, seed=seed)

        systemic = None
        if config.systemic:
            sample = sorted(network.graph.merchant_ids)[: budget.systemic_sample]
            systemic = compute_systemic_importance(
                network.graph, config=sim_config, merchants=sample
            )
            systemic_payload[network.dataset_version] = {
                "n_sampled": len(sample),
                "baseline_rank_correlation": systemic.baseline_correlations(),
                "top_by_importance": systemic.ranked(5),
                "top_by_scale": systemic.ranked_by_scale(5),
            }

        already = baseline_affected_set(network.graph, sim_config)
        # The pruning benchmark solves exactly on the *unpruned* pool, which is an
        # order of magnitude more simulations than the rest of a scenario put
        # together. Measured once per network rather than once per scenario: it
        # characterises the candidate filter, and the filter does not change
        # between the families on one graph.
        first_of_dataset = True
        for scenario in scenario_suite(
            network.graph,
            dataset_id=network.dataset_version,
            seed=seed,
            magnitude=config.magnitude,
            families=config.families,
            config=sim_config,
            baseline_affected=already,
        ):
            report.scenarios.append(
                run_scenario(
                    scenario,
                    config=config,
                    sim_config=sim_config,
                    systemic=systemic,
                    objective_settings=objective_settings,
                    measure_pruning=first_of_dataset,
                )
            )
            first_of_dataset = False

    report.systemic = systemic_payload
    report.elapsed_s = time.perf_counter() - started
    logger.info(
        "phase4_complete",
        run_id=report.run_id,
        n_scenarios=len(report.scenarios),
        elapsed_s=round(report.elapsed_s, 1),
    )
    return report


def _provenance(config: Phase4Config) -> dict[str, Any]:
    """Everything needed to reproduce, and to know what produced a number."""
    from lce.learning.features import FEATURE_SCHEMA_VERSION

    return {
        "code_version": __version__,
        "config_hash": config.config_hash,
        "seed": config.seed,
        "seeds": list(config.seeds),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "predictor": config.predictor,
        "artifact_version": config.artifact_version,
        "resource_profile": config.budget.to_dict(),
        "objective": config.objective.to_dict(),
        "uncertainty": config.uncertainty.to_dict(),
        "created_at": datetime.now(tz=UTC).isoformat(),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
        },
    }


def write_artifact(report: Phase4Report, root: Path = Path("reports/phase4")) -> Path:
    """Write ``reports/phase4/<run_id>/result.json``.

    One machine-readable file per run, holding the prediction, the intervention,
    the counterfactual, the evaluation, the timing and the provenance - so a
    number can be traced back to the decision and the configuration that produced
    it without re-running anything.
    """
    directory = Path(root) / report.run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "result.json"
    path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    logger.info("phase4_artifact_written", path=str(path))
    return path
