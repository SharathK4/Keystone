"""Stage-2: deterministic search over the pruned candidate set.

Exhaustive enumeration is affordable only at ``SMALL_FAST``. Everywhere else the
procedure is two-stage: :mod:`lce.intervention.actions` prunes to a small
feasible, explainably-ranked set, and this module searches it with the true
simulator.

Greedy on the stated objective
------------------------------
The greedy rule here maximises the marginal value of the *actual* objective:

.. math::

    \\arg\\max_u \\; \\big[ D(a) - D(a \\cup \\{u\\}) \\big] - \\lambda \\, c(u)

and stops as soon as no remaining action improves ``J``. That is deliberately
not the same rule as the Phase-1 ``GreedySearch``, which ranks by disruption
prevented *per rupee*. Both are reasonable; they optimise different things, and
mixing them would mean reporting a gap against an objective the search was never
trying to minimise. The Phase-1 rule stays available and is run as a named
comparator in the benchmark.

For the constrained form the rule flips to cost-effectiveness - the cheapest
route to the disruption ceiling - and stops the moment the ceiling is met, since
anything beyond that is capital spent for nothing.

Pruning is measured, not asserted
---------------------------------
:func:`benchmark_pruning` answers the only question that matters about a
candidate filter: *did the optimum survive it?* It computes the exact optimum on
the unpruned set and on the pruned set, and reports whether they agree, along
with the runtime actually saved. A filter that is fast and throws away the
answer is worse than no filter.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from lce.domain.intervention import Intervention
from lce.errors import OptimizationError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.intervention.exact import SolverResult, realised_floor_report, solve_exact
from lce.intervention.problem import (
    InterventionConstraints,
    ObjectiveSpec,
    check_action,
)
from lce.logging import get_logger
from lce.simulation.counterfactual import CounterfactualEvaluator

logger = get_logger(__name__)


def greedy_solve(
    evaluator: CounterfactualEvaluator,
    candidates: Sequence[Intervention],
    graph: TemporalPaymentGraph,
    *,
    constraints: InterventionConstraints,
    objective: ObjectiveSpec = ObjectiveSpec(),
    min_improvement: float = 0.0,
) -> SolverResult:
    """Add one action at a time while it improves ``J``; stop when none does.

    Every marginal value is a real simulation. The stopping rule is what keeps
    the result honest: an action that does not improve the objective is not
    taken, so the empty plan is a legitimate outcome rather than a failure.
    """
    started = time.perf_counter()
    baseline = evaluator.baseline_disruption()
    epsilon = objective.resolve_epsilon(baseline)

    chosen: list[Intervention] = []
    current_disruption = baseline
    current_cost = 0.0
    current_value = objective.value(current_disruption, current_cost)
    trace: list[dict[str, Any]] = []

    for _ in range(constraints.max_actions):
        if objective.form == "constrained" and current_disruption <= epsilon + 1e-9:
            break

        pool = [
            u
            for u in candidates
            if u not in chosen and check_action([*chosen, u], graph, constraints).feasible
        ]
        if not pool:
            break

        best: tuple[float, Intervention, float, float] | None = None
        for candidate in pool:
            augmented = [*chosen, candidate]
            disruption = evaluator.disruption(augmented)
            # The liquidity floor is a property of the run, so it is checked here
            # rather than in the structural filter above. Skipping the candidate
            # is what stops the search recommending an action it is not allowed
            # to take.
            if not realised_floor_report(evaluator, augmented, constraints).feasible:
                continue
            cost = current_cost + candidate.cost
            if objective.form == "penalised":
                value = objective.value(disruption, cost)
                improvement = current_value - value
            else:
                # Cheapest progress toward the ceiling: rupees per unit of
                # disruption removed, inverted so larger is better.
                removed = current_disruption - disruption
                improvement = removed / max(candidate.cost, 1.0)
                value = cost
            if best is None or improvement > best[0]:
                best = (improvement, candidate, disruption, cost)

        if best is None or best[0] <= min_improvement:
            break

        improvement, candidate, disruption, cost = best
        chosen.append(candidate)
        current_disruption = disruption
        current_cost = cost
        current_value = objective.value(disruption, cost)
        trace.append(
            {
                "step": len(chosen),
                "intervention_id": candidate.intervention_id,
                "description": candidate.describe(),
                "improvement": improvement,
                "disruption": disruption,
                "cost": cost,
            }
        )

    status = "GREEDY_COMPLETE"
    if objective.form == "constrained" and current_disruption > epsilon + 1e-9:
        status = "CEILING_NOT_MET"

    final = check_action(chosen, graph, constraints)
    final.violations.extend(realised_floor_report(evaluator, chosen, constraints).violations)

    return SolverResult(
        interventions=chosen,
        method="greedy_objective",
        status=status,
        feasible=final.feasible,
        objective_value=current_value,
        disruption=current_disruption,
        baseline_disruption=baseline,
        cost=current_cost,
        gap=None,  # a heuristic proves nothing about its own optimality
        runtime_s=time.perf_counter() - started,
        simulations=evaluator.simulations_run,
        n_candidates=len(candidates),
        feasibility=final,
        notes={
            "trace": trace,
            "epsilon": epsilon if objective.form == "constrained" else None,
            "objective": objective.to_dict(),
            "rule": (
                "marginal J improvement"
                if objective.form == "penalised"
                else "cheapest progress to the ceiling"
            ),
        },
    )


@dataclass(slots=True)
class PruningBenchmark:
    """What the candidate filter cost and what it saved."""

    n_before: int = 0
    n_after: int = 0
    optimum_retained: bool | None = None
    """``True`` when the pruned set contains an action achieving the unpruned
    optimum's objective value. ``None`` when the unpruned optimum was not
    affordable to compute, which is itself the honest answer."""
    objective_unpruned: float | None = None
    objective_pruned: float | None = None
    runtime_unpruned_s: float | None = None
    runtime_pruned_s: float | None = None
    simulations_unpruned: int | None = None
    simulations_pruned: int | None = None
    note: str = ""

    @property
    def reduction(self) -> float:
        if self.n_before <= 0:
            return 0.0
        return 1.0 - self.n_after / self.n_before

    @property
    def runtime_reduction(self) -> float | None:
        if not self.runtime_unpruned_s or self.runtime_pruned_s is None:
            return None
        return 1.0 - self.runtime_pruned_s / self.runtime_unpruned_s

    @property
    def objective_regret(self) -> float | None:
        """How much worse the pruned optimum is, in objective units."""
        if self.objective_unpruned is None or self.objective_pruned is None:
            return None
        return self.objective_pruned - self.objective_unpruned

    @property
    def relative_regret(self) -> float | None:
        """The same, as a fraction of what the unpruned optimum achieved.

        The boolean above is exact to a rounding tolerance, which makes it
        pessimistic: a filter that loses a fraction of a percent of the objective
        reports ``optimum_retained = False`` exactly like one that loses half.
        Both numbers are reported so the reader can tell those apart.
        """
        regret = self.objective_regret
        if regret is None:
            return None
        return regret / max(abs(self.objective_unpruned or 0.0), 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_before": self.n_before,
            "n_after": self.n_after,
            "reduction": self.reduction,
            "optimum_retained": self.optimum_retained,
            "objective_unpruned": self.objective_unpruned,
            "objective_pruned": self.objective_pruned,
            "objective_regret": self.objective_regret,
            "relative_regret": self.relative_regret,
            "runtime_unpruned_s": self.runtime_unpruned_s,
            "runtime_pruned_s": self.runtime_pruned_s,
            "runtime_reduction": self.runtime_reduction,
            "simulations_unpruned": self.simulations_unpruned,
            "simulations_pruned": self.simulations_pruned,
            "note": self.note,
        }


def benchmark_pruning(
    graph: TemporalPaymentGraph,
    shock: Any,
    unpruned: Sequence[Intervention],
    pruned: Sequence[Intervention],
    *,
    constraints: InterventionConstraints,
    objective: ObjectiveSpec,
    sim_config: Any,
    objective_settings: Any = None,
    subset_cap: int = 60_000,
    tolerance: float = 1e-6,
) -> PruningBenchmark:
    """Solve exactly on both sets and report whether the filter kept the answer.

    Two independent evaluators are used so the runtime comparison is not
    contaminated by a shared cache - the pruned solve would otherwise inherit
    every simulation the unpruned one already paid for and look far faster than
    it is.
    """
    result = PruningBenchmark(n_before=len(unpruned), n_after=len(pruned))

    try:
        evaluator = CounterfactualEvaluator(
            graph=graph, shock=shock, config=sim_config, objective=objective_settings
        )
        full = solve_exact(
            evaluator,
            unpruned,
            graph,
            constraints=constraints,
            objective=objective,
            subset_cap=subset_cap,
        )
        result.objective_unpruned = full.objective_value
        result.runtime_unpruned_s = full.runtime_s
        result.simulations_unpruned = full.simulations
    except OptimizationError as exc:
        result.note = f"unpruned optimum not affordable: {exc.message}"
        return result

    evaluator = CounterfactualEvaluator(
        graph=graph, shock=shock, config=sim_config, objective=objective_settings
    )
    small = solve_exact(
        evaluator, pruned, graph, constraints=constraints, objective=objective
    )
    result.objective_pruned = small.objective_value
    result.runtime_pruned_s = small.runtime_s
    result.simulations_pruned = small.simulations
    slack = tolerance * max(1.0, abs(full.objective_value))
    result.optimum_retained = bool(small.objective_value <= full.objective_value + slack)

    logger.info(
        "pruning_benchmarked",
        n_before=result.n_before,
        n_after=result.n_after,
        optimum_retained=result.optimum_retained,
        runtime_reduction=result.runtime_reduction,
    )
    return result


@dataclass(slots=True)
class TwoStageResult:
    """Stage-1 pruning plus the Stage-2 solve it fed."""

    solver: SolverResult
    n_generated: int = 0
    n_feasible: int = 0
    n_retained: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    stage1_runtime_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage1": {
                "n_generated": self.n_generated,
                "n_feasible": self.n_feasible,
                "n_retained": self.n_retained,
                "rejected_by_constraint": dict(self.rejected),
                "runtime_s": round(self.stage1_runtime_s, 4),
            },
            "stage2": self.solver.to_dict(),
        }
