"""Counterfactual evaluation of intervention plans.

The optimiser needs ``D(G, S, U)`` many times over. This module owns that call,
plus the caching that keeps a greedy or exhaustive search affordable: plans are
keyed by their sorted intervention ids, so re-evaluating a set the search has
already seen costs nothing.

The baseline ``D(G, S, {})`` is computed once per evaluator and reused, which
matters because every marginal-gain calculation references it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lce.config import ObjectiveSettings
from lce.domain.intervention import Intervention, InterventionPlan
from lce.domain.propagation import CascadeResult
from lce.domain.shock import Shock
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.simulation.engine import LiquiditySimulator, SimulationConfig


@dataclass(slots=True)
class CounterfactualEvaluator:
    """Evaluates intervention plans against a fixed (graph, shock) pair."""

    graph: TemporalPaymentGraph
    shock: Shock
    config: SimulationConfig = field(default_factory=SimulationConfig)
    objective: ObjectiveSettings | None = None

    _cache: dict[tuple[str, ...], CascadeResult] = field(default_factory=dict, repr=False)
    _baseline: CascadeResult | None = field(default=None, repr=False)
    _sim_count: int = field(default=0, repr=False)

    def _simulator(self) -> LiquiditySimulator:
        return LiquiditySimulator(self.graph, self.config, self.objective)

    @staticmethod
    def _key(interventions: list[Intervention]) -> tuple[str, ...]:
        return tuple(sorted(u.intervention_id for u in interventions))

    def baseline(self) -> CascadeResult:
        """``D(G, S, {})`` - the do-nothing cascade. Computed once, then cached."""
        if self._baseline is None:
            self._sim_count += 1
            self._baseline = self._simulator().run(self.shock, run_id="baseline")
        return self._baseline

    def baseline_disruption(self) -> float:
        return self.baseline().disruption or 0.0

    def evaluate(self, interventions: list[Intervention]) -> CascadeResult:
        """``D(G, S, U)`` for an arbitrary intervention set."""
        if not interventions:
            return self.baseline()
        key = self._key(interventions)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        plan = InterventionPlan(interventions=list(interventions))
        self._sim_count += 1
        # Deterministic run id keyed by the plan, so common random numbers line
        # up across every counterfactual and the baseline.
        result = self._simulator().run(self.shock, plan, run_id="counterfactual")
        self._cache[key] = result
        return result

    def disruption(self, interventions: list[Intervention]) -> float:
        return self.evaluate(interventions).disruption or 0.0

    def marginal_gain(
        self, interventions: list[Intervention], candidate: Intervention
    ) -> float:
        """``D(U) - D(U + {u})``: disruption prevented by adding ``candidate``."""
        current = self.disruption(interventions)
        augmented = self.disruption([*interventions, candidate])
        return current - augmented

    def evaluate_plan(self, plan: InterventionPlan) -> InterventionPlan:
        """Attach measured baseline/residual disruption to a plan."""
        result = self.evaluate(plan.interventions)
        return plan.with_evaluation(
            baseline=self.baseline_disruption(),
            residual=result.disruption or 0.0,
            optimizer=plan.optimizer or "manual",
        )

    @property
    def simulations_run(self) -> int:
        """How many full simulations this evaluator has executed."""
        return self._sim_count

    def reset_cache(self) -> None:
        self._cache.clear()
        self._baseline = None
        self._sim_count = 0
