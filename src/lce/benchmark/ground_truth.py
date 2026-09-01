"""Latent ground truth for a benchmark scenario.

The benchmark's whole value rests on one discipline: the generator and the
simulator know things the model must infer, and those things are kept strictly
on this side of the wall.

What is hidden
--------------
* the true dependency edges and their strengths / lag laws (drawn by the
  generator, never observable in the event stream);
* the true liquidity trajectory of every merchant;
* which node was actually shocked, and by how much;
* who was actually affected, when, and how deep in the cascade;
* the disrupted payment volume;
* the feasible intervention set and, on small networks, the true optimum.

:meth:`ScenarioGroundTruth.observable_graph` returns the *only* view a model is
allowed to consume: the network with its dependency overlay stripped, so
dependencies must be learned from payments rather than read off. Handing a model
``scenario.graph`` directly would leak the generator's parameters and turn every
downstream metric into a self-fulfilling measurement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from lce.benchmark.scenarios import BuiltScenario
from lce.config import ObjectiveSettings
from lce.domain.edges import DependencyEdge
from lce.domain.intervention import Intervention
from lce.domain.propagation import CascadeResult
from lce.errors import OptimizationError
from lce.evaluation.metrics import attributable_affected
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
from lce.optimization.candidates import CandidateConfig, generate_candidates
from lce.optimization.search import ExhaustiveSearch, SearchConfig
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import LiquiditySimulator, SimulationConfig

logger = get_logger(__name__)

# Above this many candidates, exhaustive search over subsets is not affordable
# and the "true optimum" is reported as unavailable rather than approximated.
MAX_CANDIDATES_FOR_EXACT_OPTIMUM = 14


@dataclass(slots=True)
class TrueLiquidityState:
    """A merchant's true liquidity, opening and as realised under the shock."""

    merchant_id: str
    opening_balance: float
    operating_floor: float
    credit_limit: float
    opening_buffer: float
    min_buffer: float
    final_balance: float
    deficit_integral: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "opening_balance": self.opening_balance,
            "operating_floor": self.operating_floor,
            "credit_limit": self.credit_limit,
            "opening_buffer": self.opening_buffer,
            "min_buffer": self.min_buffer,
            "final_balance": self.final_balance,
            "deficit_integral": self.deficit_integral,
        }


@dataclass(slots=True)
class TrueOptimum:
    """The best intervention plan, found by exhaustive counterfactual search."""

    available: bool
    reason: str = ""
    interventions: list[dict[str, Any]] = field(default_factory=list)
    cost: float = 0.0
    residual_disruption: float | None = None
    baseline_disruption: float | None = None
    subsets_evaluated: int = 0
    search_ms: float = 0.0

    @property
    def disruption_prevented(self) -> float | None:
        if self.residual_disruption is None or self.baseline_disruption is None:
            return None
        return self.baseline_disruption - self.residual_disruption

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "interventions": self.interventions,
            "cost": self.cost,
            "residual_disruption": self.residual_disruption,
            "baseline_disruption": self.baseline_disruption,
            "disruption_prevented": self.disruption_prevented,
            "subsets_evaluated": self.subsets_evaluated,
            "search_ms": self.search_ms,
        }


@dataclass(slots=True)
class ScenarioGroundTruth:
    """Everything the benchmark knows and the model must not see."""

    scenario_id: str
    dataset_id: str
    family: str

    # --- structural truth (from the generator) ----------------------------
    true_edges: dict[tuple[str, str], DependencyEdge] = field(default_factory=dict)

    # --- shock truth -------------------------------------------------------
    shock_source: list[str] = field(default_factory=list)
    shock_magnitude: float = 0.0
    shock_time: float = 0.0

    # --- cascade truth -----------------------------------------------------
    affected_nodes: list[str] = field(default_factory=list)
    first_constraint_t: dict[str, float] = field(default_factory=dict)
    cascade_depth: dict[str, int] = field(default_factory=dict)
    max_cascade_depth: int = 0
    disrupted_volume: float = 0.0
    defaulted_nodes: list[str] = field(default_factory=list)
    attributable_disruption: float = 0.0
    baseline_disruption: float = 0.0
    shocked_disruption: float = 0.0

    # --- liquidity truth ---------------------------------------------------
    liquidity_states: dict[str, TrueLiquidityState] = field(default_factory=dict)

    # --- intervention truth ------------------------------------------------
    feasible_interventions: list[dict[str, Any]] = field(default_factory=list)
    optimal_intervention: TrueOptimum = field(default_factory=lambda: TrueOptimum(False))

    horizon_hours: float = 168.0
    seed: int = 0

    # Retained so callers can re-derive anything; excluded from serialisation.
    _graph: TemporalPaymentGraph | None = field(default=None, repr=False)

    # --------------------------------------------------------------- views

    def observable_graph(self) -> TemporalPaymentGraph:
        """The only view a model may consume: dependencies stripped.

        The generator installs the true dependency overlay on the graph it
        builds. Serving that to a learner would hand it the answer, so this
        returns a copy with the overlay cleared - payments and obligations
        remain, and the dependency structure has to be inferred from them.
        """
        if self._graph is None:
            raise ValueError("ground truth was constructed without a graph reference")
        observable = self._graph.copy()
        observable.clear_dependencies()
        return observable

    def true_dependency_strength(self) -> dict[tuple[str, str], float]:
        return {k: e.pass_through for k, e in self.true_edges.items()}

    def true_lag_hours(self) -> dict[tuple[str, str], float]:
        return {k: e.lag.mean_hours for k, e in self.true_edges.items()}

    def to_dict(self, *, include_edges: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scenario_id": self.scenario_id,
            "dataset_id": self.dataset_id,
            "family": self.family,
            "horizon_hours": self.horizon_hours,
            "seed": self.seed,
            "shock": {
                "source": self.shock_source,
                "magnitude": self.shock_magnitude,
                "time": self.shock_time,
            },
            "cascade": {
                "affected_nodes": self.affected_nodes,
                "n_affected": len(self.affected_nodes),
                "first_constraint_t": self.first_constraint_t,
                "cascade_depth": self.cascade_depth,
                "max_cascade_depth": self.max_cascade_depth,
                "defaulted_nodes": self.defaulted_nodes,
                "disrupted_volume": self.disrupted_volume,
                "attributable_disruption": self.attributable_disruption,
                "baseline_disruption": self.baseline_disruption,
                "shocked_disruption": self.shocked_disruption,
            },
            "liquidity_states": {
                k: v.to_dict() for k, v in self.liquidity_states.items()
            },
            "interventions": {
                "n_feasible": len(self.feasible_interventions),
                "feasible": self.feasible_interventions,
                "optimal": self.optimal_intervention.to_dict(),
            },
        }
        if include_edges:
            payload["true_edges"] = [
                {
                    "source_id": source,
                    "target_id": target,
                    "pass_through": edge.pass_through,
                    "conditional_probability": edge.conditional_probability,
                    "reliability": edge.reliability,
                    "lag_mean_hours": edge.lag.mean_hours,
                    "lag_mu_log": edge.lag.mu_log,
                    "lag_sigma_log": edge.lag.sigma_log,
                }
                for (source, target), edge in sorted(self.true_edges.items())
            ]
        return payload

    def summary(self) -> dict[str, Any]:
        """Compact view for CLI output and logs."""
        return {
            "scenario_id": self.scenario_id,
            "family": self.family,
            "shock_source": self.shock_source,
            "n_affected": len(self.affected_nodes),
            "max_cascade_depth": self.max_cascade_depth,
            "disrupted_volume": round(self.disrupted_volume, 2),
            "attributable_disruption": round(self.attributable_disruption, 2),
            "n_feasible_interventions": len(self.feasible_interventions),
            "optimum_available": self.optimal_intervention.available,
        }


def compute_ground_truth(
    scenario: BuiltScenario,
    *,
    true_edges: dict[tuple[str, str], DependencyEdge] | None = None,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
    baseline: CascadeResult | None = None,
    compute_optimum: bool = True,
    max_candidates_for_optimum: int = MAX_CANDIDATES_FOR_EXACT_OPTIMUM,
    budget: float | None = None,
    max_actions: int = 2,
) -> ScenarioGroundTruth:
    """Simulate a scenario and record everything the model must not see.

    ``baseline`` is accepted so a suite over many scenarios on one network
    computes the no-shock run once; it does not depend on the shock. Note the
    baseline must be run on the *scenario's* graph, since families like
    ``DELAYED_INFLOW`` mutate obligations before the run.
    """
    sim_config = config or SimulationConfig()
    graph = scenario.graph

    # The baseline runs on the *unperturbed* network. Mutation-based families
    # (delayed inflow, supplier failure) encode their shock in the obligation
    # book, so simulating the baseline on the mutated graph would include the
    # perturbation on both sides of the difference and report no impact.
    base = baseline or LiquiditySimulator(
        scenario.unperturbed_graph, sim_config, objective
    ).run(None, run_id=f"{scenario.scenario_id}:baseline")
    shocked = LiquiditySimulator(graph, sim_config, objective).run(
        scenario.shock, run_id=f"{scenario.scenario_id}:shocked"
    )

    affected = attributable_affected(shocked.affected_ids, base.affected_ids)
    affected_set = set(affected)

    first_constraint = {
        m: t for m, t in shocked.hit_times().items() if m in affected_set
    }
    depth = {
        m: o.hop_distance
        for m, o in shocked.outcomes.items()
        if m in affected_set and o.hop_distance is not None
    }
    # Disrupted volume is the *incremental* value delayed because of the
    # scenario, accumulated per merchant and counting only increases.
    #
    # Two simpler definitions both fail. Summing the affected nodes' absolute
    # value_delayed misses damage landing on merchants that were already late
    # for their own reasons. Taking one network-wide difference lets relief
    # cancel harm: extending a deadline genuinely lets the debtor off, and that
    # credit would net out the creditor's loss, reporting zero disruption for a
    # scenario that demonstrably broke someone.
    disrupted_volume = sum(
        max(0.0, outcome.value_delayed - base.outcomes[merchant_id].value_delayed)
        for merchant_id, outcome in shocked.outcomes.items()
        if merchant_id in base.outcomes
    )

    liquidity = {}
    for merchant_id, outcome in shocked.outcomes.items():
        profile = graph.merchant(merchant_id)
        liquidity[merchant_id] = TrueLiquidityState(
            merchant_id=merchant_id,
            opening_balance=profile.opening_balance,
            operating_floor=profile.operating_floor,
            credit_limit=profile.credit_limit,
            opening_buffer=profile.initial_buffer,
            min_buffer=outcome.min_buffer,
            final_balance=outcome.final_balance,
            deficit_integral=outcome.deficit_integral,
        )

    feasible, optimum = _intervention_truth(
        scenario,
        sim_config,
        objective,
        compute_optimum=compute_optimum,
        max_candidates=max_candidates_for_optimum,
        budget=budget,
        max_actions=max_actions,
    )

    truth = ScenarioGroundTruth(
        scenario_id=scenario.scenario_id,
        dataset_id=scenario.dataset_id,
        family=str(scenario.spec.family),
        true_edges=dict(true_edges or {}),
        shock_source=list(scenario.shock.origin_ids),
        shock_magnitude=scenario.shock.total_magnitude,
        shock_time=scenario.shock.onset_t,
        affected_nodes=affected,
        first_constraint_t=first_constraint,
        cascade_depth=depth,
        max_cascade_depth=max(depth.values()) if depth else 0,
        disrupted_volume=disrupted_volume,
        defaulted_nodes=shocked.defaulted_ids,
        attributable_disruption=max(
            0.0, (shocked.disruption or 0.0) - (base.disruption or 0.0)
        ),
        baseline_disruption=base.disruption or 0.0,
        shocked_disruption=shocked.disruption or 0.0,
        liquidity_states=liquidity,
        feasible_interventions=feasible,
        optimal_intervention=optimum,
        horizon_hours=sim_config.horizon_hours,
        seed=sim_config.seed,
        _graph=graph,
    )
    logger.info("ground_truth_computed", **truth.summary())
    return truth


def _intervention_truth(
    scenario: BuiltScenario,
    sim_config: SimulationConfig,
    objective: ObjectiveSettings | None,
    *,
    compute_optimum: bool,
    max_candidates: int,
    budget: float | None,
    max_actions: int,
) -> tuple[list[dict[str, Any]], TrueOptimum]:
    """Enumerate feasible interventions and, if affordable, the true optimum."""
    graph = scenario.graph
    prediction = LinearThresholdPropagator(
        PropagationConfig(horizon_hours=sim_config.horizon_hours)
    ).predict(graph, scenario.shock)
    candidate_set = generate_candidates(
        graph,
        scenario.shock,
        prediction,
        CandidateConfig(top_k_nodes=6, max_candidates=max_candidates),
        horizon_hours=sim_config.horizon_hours,
    )

    feasible = [
        _intervention_payload(u)
        for u in candidate_set.interventions
        if budget is None or u.cost <= budget + 1e-6
    ]

    if not compute_optimum or not candidate_set.interventions:
        return feasible, TrueOptimum(
            available=False,
            reason="optimum not requested" if not compute_optimum else "no candidates",
        )
    if len(candidate_set.interventions) > max_candidates:
        return feasible, TrueOptimum(
            available=False,
            reason=(
                f"{len(candidate_set.interventions)} candidates exceeds the exact-search "
                f"cap of {max_candidates}; no true optimum is claimed"
            ),
        )

    started = time.perf_counter()
    evaluator = CounterfactualEvaluator(
        graph=graph, shock=scenario.shock, config=sim_config, objective=objective
    )
    try:
        result = ExhaustiveSearch().run(
            evaluator,
            candidate_set.interventions,
            SearchConfig(budget=budget, max_actions=max_actions),
        )
    except OptimizationError as exc:
        return feasible, TrueOptimum(available=False, reason=exc.message)

    return feasible, TrueOptimum(
        available=True,
        interventions=[_intervention_payload(u) for u in result.plan.interventions],
        cost=result.cost,
        residual_disruption=result.achieved_disruption,
        baseline_disruption=result.baseline_disruption,
        subsets_evaluated=int(result.notes.get("subsets_enumerated", 0)),
        search_ms=(time.perf_counter() - started) * 1000.0,
    )


def _intervention_payload(action: Intervention) -> dict[str, Any]:
    return {
        "intervention_id": action.intervention_id,
        "type": str(action.type),
        "merchant_id": action.merchant_id,
        "t": action.t,
        "amount": action.amount,
        "shift_hours": action.shift_hours,
        "tranches": action.tranches,
        "target_obligation_id": action.target_obligation_id,
        "cost": action.cost,
        "description": action.describe(),
    }
