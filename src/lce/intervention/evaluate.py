"""True counterfactual evaluation: replay the decision, report what happened.

The discipline this module exists to enforce is one sentence long: **a model-
selected intervention is never scored on the model's own predicted outcome.**
The prediction is what chose the action; the simulator is what says whether it
worked, and only the simulator's number is reported.

The protocol, in order
----------------------
1. predict the cascade from observable pre-shock information (Phase-3 barrier);
2. select an action with the model-based procedure;
3. **replay that action in the true simulator**;
4. compare against no intervention, the naive rules, and the exact optimum where
   one is affordable.

Every strategy is replayed the same way, so the comparison isolates the
*selection* rather than the evaluation.

What is reported, and why each number is there
----------------------------------------------
``true_disruption``        what the simulator produced. The headline.
``predicted_disruption``   what the selecting model expected, kept alongside so
                           prediction error is visible rather than absorbed.
``commerce_preserved``     INR of payment value that moves on time under the plan
                           and did not without it. A financial quantity, not an
                           index.
``capital_efficiency``     disruption prevented per rupee deployed.
``absolute_gap``           ``J(a) - J(a*)`` against the exact optimum.
``relative_gap``           the same, scaled by what the optimum achieved.
``regret``                 disruption the optimum prevented and this strategy did
                           not - the operational cost of not being optimal.
``feasibility_violations`` named constraints broken, including money creation.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from lce.config import ObjectiveSettings
from lce.domain.events import Obligation
from lce.domain.intervention import Intervention, InterventionPlan
from lce.domain.propagation import CascadeResult
from lce.domain.shock import Shock
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.intervention.problem import (
    FeasibilityReport,
    InterventionConstraints,
    ObjectiveSpec,
    check_action,
    check_conservation,
    check_realised_floor,
)
from lce.logging import get_logger
from lce.simulation.engine import LiquiditySimulator, SimulationConfig

logger = get_logger(__name__)


@dataclass(slots=True)
class Replay:
    """One simulator run of one action, with everything it produced."""

    cascade: CascadeResult
    obligations: list[Obligation]
    disruption: float
    value_delayed: float
    n_affected: int
    n_defaulted: int
    runtime_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "disruption": self.disruption,
            "value_delayed": self.value_delayed,
            "n_affected": self.n_affected,
            "n_defaulted": self.n_defaulted,
            "max_hop": self.cascade.max_hop(),
            "runtime_s": round(self.runtime_s, 4),
        }


def replay(
    graph: TemporalPaymentGraph,
    shock: Shock,
    interventions: Sequence[Intervention],
    *,
    config: SimulationConfig,
    objective_settings: ObjectiveSettings | None = None,
    run_id: str = "replay",
) -> Replay:
    """Run the true simulator on ``(graph, shock, interventions)``.

    Uses the simulator directly rather than the cached evaluator so the final
    obligation book comes back too - the conservation check needs the realised
    book, and a cached disruption scalar cannot supply it.
    """
    started = time.perf_counter()
    simulator = LiquiditySimulator(graph, config, objective_settings)
    plan = InterventionPlan(interventions=list(interventions)) if interventions else None
    cascade = simulator.run(shock, plan, run_id=run_id)
    return Replay(
        cascade=cascade,
        obligations=simulator.obligation_book(),
        disruption=cascade.disruption or 0.0,
        value_delayed=sum(o.value_delayed for o in cascade.outcomes.values()),
        n_affected=len(cascade.affected_ids),
        n_defaulted=len(cascade.defaulted_ids),
        runtime_s=time.perf_counter() - started,
    )


@dataclass(slots=True)
class StrategyOutcome:
    """One strategy's decision and what the simulator did with it."""

    name: str
    interventions: list[Intervention] = field(default_factory=list)
    predicted_disruption: float | None = None
    true_disruption: float = 0.0
    baseline_disruption: float = 0.0
    cost: float = 0.0
    commerce_preserved: float = 0.0
    n_affected: int = 0
    n_defaulted: int = 0
    feasibility: FeasibilityReport = field(default_factory=FeasibilityReport)
    conservation: FeasibilityReport = field(default_factory=FeasibilityReport)
    floor: FeasibilityReport = field(default_factory=FeasibilityReport)
    selection_runtime_s: float = 0.0
    replay_runtime_s: float = 0.0
    simulations: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    # Filled in once the reference optimum is known.
    absolute_gap: float | None = None
    relative_gap: float | None = None
    regret: float | None = None

    @property
    def disruption_prevented(self) -> float:
        return self.baseline_disruption - self.true_disruption

    @property
    def disruption_reduction_pct(self) -> float:
        if self.baseline_disruption <= 0:
            return 0.0
        return 100.0 * self.disruption_prevented / self.baseline_disruption

    @property
    def capital_efficiency(self) -> float:
        if self.cost <= 0:
            return float("inf") if self.disruption_prevented > 0 else 0.0
        return self.disruption_prevented / self.cost

    @property
    def prediction_error(self) -> float | None:
        """Signed: positive means the model expected more damage than occurred."""
        if self.predicted_disruption is None:
            return None
        return self.predicted_disruption - self.true_disruption

    @property
    def violations(self) -> list[str]:
        return sorted(
            set(self.feasibility.names())
            | set(self.conservation.names())
            | set(self.floor.names())
        )

    def objective_value(self, objective: ObjectiveSpec) -> float:
        return objective.value(self.true_disruption, self.cost)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.name,
            "n_actions": len(self.interventions),
            "interventions": [
                {
                    "intervention_id": u.intervention_id,
                    "type": str(u.type),
                    "merchant_id": u.merchant_id,
                    "t": u.t,
                    "amount": u.amount,
                    "cost": u.cost,
                    "description": u.describe(),
                    "provenance": u.provenance,
                }
                for u in self.interventions
            ],
            "predicted_disruption": self.predicted_disruption,
            "true_disruption": self.true_disruption,
            "baseline_disruption": self.baseline_disruption,
            "prediction_error": self.prediction_error,
            "disruption_prevented": self.disruption_prevented,
            "disruption_reduction_pct": self.disruption_reduction_pct,
            "cost": self.cost,
            "capital_efficiency": _finite(self.capital_efficiency),
            "commerce_preserved": self.commerce_preserved,
            "n_affected": self.n_affected,
            "n_defaulted": self.n_defaulted,
            "absolute_gap": self.absolute_gap,
            "relative_gap": self.relative_gap,
            "regret": self.regret,
            "feasibility_violations": self.violations,
            "feasibility": self.feasibility.to_dict(),
            "conservation": self.conservation.to_dict(),
            "liquidity_floor": self.floor.to_dict(),
            "selection_runtime_s": round(self.selection_runtime_s, 4),
            "replay_runtime_s": round(self.replay_runtime_s, 4),
            "simulations": self.simulations,
            "notes": self.notes,
        }


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


@dataclass(slots=True)
class CounterfactualReport:
    """Every strategy on one scenario, with the reference optimum if there is one."""

    scenario_id: str = ""
    dataset_id: str = ""
    family: str = ""
    outcomes: list[StrategyOutcome] = field(default_factory=list)
    reference: str | None = None
    """Name of the strategy treated as the optimum. ``None`` when none was
    affordable, in which case every gap is ``None`` rather than measured against
    a heuristic pretending to be one."""
    objective: ObjectiveSpec = field(default_factory=ObjectiveSpec)

    def by_name(self, name: str) -> StrategyOutcome | None:
        for outcome in self.outcomes:
            if outcome.name == name:
                return outcome
        return None

    def ranked(self) -> list[StrategyOutcome]:
        return sorted(self.outcomes, key=lambda o: (o.objective_value(self.objective), o.name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "dataset_id": self.dataset_id,
            "family": self.family,
            "reference": self.reference,
            "objective": self.objective.to_dict(),
            "strategies": [o.to_dict() for o in self.outcomes],
            "ranking": [o.name for o in self.ranked()],
        }


def score_against_reference(
    report: CounterfactualReport, reference: str, objective: ObjectiveSpec
) -> None:
    """Fill in gap and regret for every strategy against a named optimum.

    Gaps are only computed when the reference really is an optimum. Passing a
    heuristic here would produce numbers that look like optimality gaps and are
    not, which is worse than reporting nothing.
    """
    optimum = report.by_name(reference)
    if optimum is None:
        return
    report.reference = reference
    best_value = optimum.objective_value(objective)
    best_prevented = optimum.disruption_prevented

    for outcome in report.outcomes:
        value = outcome.objective_value(objective)
        outcome.absolute_gap = value - best_value
        denominator = max(abs(best_value), 1.0)
        outcome.relative_gap = outcome.absolute_gap / denominator
        outcome.regret = max(0.0, best_prevented - outcome.disruption_prevented)


def build_outcome(
    name: str,
    interventions: Sequence[Intervention],
    *,
    graph: TemporalPaymentGraph,
    shock: Shock,
    config: SimulationConfig,
    constraints: InterventionConstraints,
    no_intervention: Replay,
    objective_settings: ObjectiveSettings | None = None,
    predicted_disruption: float | None = None,
    selection_runtime_s: float = 0.0,
    simulations: int = 0,
    notes: dict[str, Any] | None = None,
) -> StrategyOutcome:
    """Replay one strategy's action and score it against the do-nothing run.

    ``no_intervention`` is passed in rather than recomputed: it is the same run
    for every strategy on a scenario, and re-running it per strategy would both
    waste simulations and risk the baselines drifting apart.
    """
    actions = list(interventions)
    replayed = (
        replay(
            graph,
            shock,
            actions,
            config=config,
            objective_settings=objective_settings,
            run_id=f"replay:{name}",
        )
        if actions
        else no_intervention
    )

    return StrategyOutcome(
        name=name,
        interventions=actions,
        predicted_disruption=predicted_disruption,
        true_disruption=replayed.disruption,
        baseline_disruption=no_intervention.disruption,
        cost=sum(u.cost for u in actions),
        # Value that was going to be delivered late and now is not. Positive is
        # good; a negative number means the action delayed *more* commerce than
        # it saved, which a term-extension can genuinely do.
        commerce_preserved=no_intervention.value_delayed - replayed.value_delayed,
        n_affected=replayed.n_affected,
        n_defaulted=replayed.n_defaulted,
        feasibility=check_action(actions, graph, constraints) if actions else FeasibilityReport(),
        conservation=check_conservation(
            no_intervention.obligations, replayed.obligations, actions
        ),
        floor=(
            check_realised_floor(
                replayed.cascade.outcomes, no_intervention.cascade.outcomes
            )
            if constraints.enforce_liquidity_floor
            else FeasibilityReport()
        ),
        selection_runtime_s=selection_runtime_s,
        replay_runtime_s=replayed.runtime_s,
        simulations=simulations,
        notes=notes or {},
    )


def summarise(report: CounterfactualReport) -> dict[str, Any]:
    """Compact table for logs and the CLI."""
    return {
        "scenario_id": report.scenario_id,
        "family": report.family,
        "reference": report.reference,
        "strategies": [
            {
                "strategy": o.name,
                "true_disruption": round(o.true_disruption, 2),
                "reduction_pct": round(o.disruption_reduction_pct, 2),
                "cost": round(o.cost, 2),
                "capital_efficiency": _finite(o.capital_efficiency),
                "relative_gap": o.relative_gap,
                "regret": o.regret,
                "violations": o.violations,
            }
            for o in report.ranked()
        ],
    }
