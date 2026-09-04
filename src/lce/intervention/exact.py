"""Exact and near-exact solvers.

Two solvers, and the difference between them is what each is entitled to claim.

:func:`solve_exact` - the scientific ground truth
-------------------------------------------------
Complete enumeration over feasible subsets, **each evaluated by the true
simulator**, then the argmin of the objective. That is exact because ``D`` is a
black box: it is produced by a discrete-event simulation with no algebraic
structure to exploit, so completeness is the only available proof of optimality.
A MILP over ``D`` itself does not exist to be written.

The empty set is always in the running. Doing nothing is feasible, costs
nothing, and at a high enough ``lambda`` it is genuinely optimal - a solver that
cannot return "do not intervene" is not solving the stated problem.

:func:`solve_milp` - near-exact, and honest about the surrogate
---------------------------------------------------------------
When enumeration is unaffordable but ``O(n^2)`` simulations are not, a CP-SAT
model is solved over a **measured pairwise surrogate**:

.. math::

    \\hat{D}(a) = D(\\emptyset) - \\sum_i g_i x_i + \\sum_{i<j} r_{ij} x_i x_j

``g_i`` is the simulated singleton gain and ``r_{ij} = g_i + g_j - g_{ij}`` the
simulated interaction residual - positive when two actions overlap and their
combined effect is less than the sum of their parts. Products are linearised the
standard way. This is exact *for the surrogate*, and strictly better than a
linear one, which is the same model with ``r_ij`` assumed zero.

The surrogate never appears in a reported number. The chosen set is always
re-simulated, and the value that comes back is the value that is reported; the
surrogate's own objective is kept only as a solver diagnostic.
"""

from __future__ import annotations

import itertools
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import comb
from typing import Any

from lce.domain.intervention import Intervention
from lce.errors import DependencyUnavailableError, OptimizationError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.intervention.problem import (
    FeasibilityReport,
    InterventionConstraints,
    ObjectiveSpec,
    check_action,
    check_realised_floor,
)
from lce.logging import get_logger
from lce.simulation.counterfactual import CounterfactualEvaluator

logger = get_logger(__name__)

#: Refuse to enumerate beyond this. Exhaustive search exists to *measure* the
#: gap on small instances, not to be the production path.
EXACT_SUBSET_CAP = 100_000

#: Above this many candidates the pairwise surrogate's ``O(n^2)`` simulations
#: stop being affordable on a laptop and the MILP falls back to singletons only.
PAIRWISE_CANDIDATE_CAP = 24


@dataclass(slots=True)
class SolverResult:
    """A solved plan, with everything needed to judge the solve itself."""

    interventions: list[Intervention] = field(default_factory=list)
    method: str = ""
    status: str = "UNKNOWN"
    feasible: bool = False
    objective_value: float = float("inf")
    disruption: float = float("inf")
    baseline_disruption: float = 0.0
    cost: float = 0.0
    gap: float | None = None
    """Solver optimality gap where the solver reports one. ``0.0`` for a
    completed enumeration, since it proved optimality by exhausting the space."""
    runtime_s: float = 0.0
    simulations: int = 0
    subsets_evaluated: int = 0
    n_candidates: int = 0
    feasibility: FeasibilityReport = field(default_factory=FeasibilityReport)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def disruption_prevented(self) -> float:
        return self.baseline_disruption - self.disruption

    @property
    def capital_efficiency(self) -> float:
        """Disruption prevented per rupee. ``inf`` for a free, effective plan."""
        if self.cost <= 0.0:
            return float("inf") if self.disruption_prevented > 0 else 0.0
        return self.disruption_prevented / self.cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status,
            "feasible": self.feasible,
            "n_actions": len(self.interventions),
            "objective_value": self.objective_value,
            "disruption": self.disruption,
            "baseline_disruption": self.baseline_disruption,
            "disruption_prevented": self.disruption_prevented,
            "cost": self.cost,
            "capital_efficiency": _finite(self.capital_efficiency),
            "gap": self.gap,
            "runtime_s": round(self.runtime_s, 4),
            "simulations": self.simulations,
            "subsets_evaluated": self.subsets_evaluated,
            "n_candidates": self.n_candidates,
            "feasibility": self.feasibility.to_dict(),
            "interventions": [
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
                    "provenance": u.provenance,
                }
                for u in self.interventions
            ],
            "notes": self.notes,
        }


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def realised_floor_report(
    evaluator: CounterfactualEvaluator,
    chosen: Sequence[Intervention],
    constraints: InterventionConstraints,
) -> FeasibilityReport:
    """Liquidity-floor feasibility for a candidate set, from its simulated run.

    The floor constraint cannot be checked structurally - whether an action
    pushes a merchant deeper below its operating floor is a property of the
    realised trajectory, not of the action's parameters. A search that only
    checks structural feasibility therefore *chooses* infeasible plans and only
    discovers it at evaluation time, which is how a term extension that moves a
    payable into a worse week ends up being recommended.

    Free to call inside a search: the evaluator has already simulated this set to
    get its disruption, and the result is cached.
    """
    if not constraints.enforce_liquidity_floor or not chosen:
        return FeasibilityReport()
    return check_realised_floor(
        evaluator.evaluate(list(chosen)).outcomes, evaluator.baseline().outcomes
    )


def count_subsets(n: int, k: int) -> int:
    return sum(comb(n, size) for size in range(0, min(k, n) + 1))


def solve_exact(
    evaluator: CounterfactualEvaluator,
    candidates: Sequence[Intervention],
    graph: TemporalPaymentGraph,
    *,
    constraints: InterventionConstraints,
    objective: ObjectiveSpec = ObjectiveSpec(),
    subset_cap: int = EXACT_SUBSET_CAP,
) -> SolverResult:
    """Complete enumeration with simulator evaluation. The reference optimum.

    Raises when the space is too large to enumerate rather than silently
    truncating - a truncated enumeration is a heuristic, and calling it an
    optimum would poison every optimality gap measured against it.
    """
    started = time.perf_counter()
    n = len(candidates)
    k = min(constraints.max_actions, n)
    total = count_subsets(n, k)
    if total > subset_cap:
        raise OptimizationError(
            f"exact search would evaluate {total:,} subsets (cap {subset_cap:,}); "
            "reduce the candidate set or max_actions",
            n_candidates=n,
            max_actions=k,
        )

    baseline = evaluator.baseline_disruption()
    epsilon = objective.resolve_epsilon(baseline)

    best: tuple[float, list[Intervention], float, float] | None = None
    evaluated = 0
    for size in range(0, k + 1):
        for subset in itertools.combinations(candidates, size):
            chosen = list(subset)
            if chosen and not check_action(chosen, graph, constraints).feasible:
                continue
            evaluated += 1
            disruption = evaluator.disruption(chosen)
            if not realised_floor_report(evaluator, chosen, constraints).feasible:
                continue
            cost = sum(u.cost for u in chosen)
            if objective.form == "constrained" and disruption > epsilon + 1e-9:
                continue
            value = objective.value(disruption, cost)
            key = (value, cost, len(chosen))
            if best is None or key < (best[0], best[3], len(best[1])):
                best = (value, chosen, disruption, cost)

    runtime = time.perf_counter() - started
    if best is None:
        # Only reachable in the constrained form, when nothing meets the ceiling.
        return SolverResult(
            method="exact_enumeration",
            status="INFEASIBLE",
            feasible=False,
            baseline_disruption=baseline,
            runtime_s=runtime,
            simulations=evaluator.simulations_run,
            subsets_evaluated=evaluated,
            n_candidates=n,
            notes={
                "reason": f"no feasible action reaches D <= {epsilon:,.0f}",
                "epsilon": epsilon,
                "objective": objective.to_dict(),
            },
        )

    value, chosen, disruption, cost = best
    return SolverResult(
        interventions=chosen,
        method="exact_enumeration",
        status="OPTIMAL",
        feasible=True,
        objective_value=value,
        disruption=disruption,
        baseline_disruption=baseline,
        cost=cost,
        gap=0.0,
        runtime_s=runtime,
        simulations=evaluator.simulations_run,
        subsets_evaluated=evaluated,
        n_candidates=n,
        feasibility=check_action(chosen, graph, constraints),
        notes={
            "epsilon": epsilon if objective.form == "constrained" else None,
            "objective": objective.to_dict(),
            "proof": "complete enumeration of the feasible subset lattice",
        },
    )


# --------------------------------------------------------------------- surrogate


@dataclass(slots=True)
class PairwiseSurrogate:
    """Measured singleton gains and interaction residuals."""

    gains: list[float] = field(default_factory=list)
    residuals: dict[tuple[int, int], float] = field(default_factory=dict)
    baseline: float = 0.0
    simulations: int = 0
    pairwise: bool = True

    def predict(self, indices: Sequence[int]) -> float:
        """Surrogate disruption for a subset. Diagnostic only - never reported."""
        value = self.baseline - sum(self.gains[i] for i in indices)
        for a, b in itertools.combinations(sorted(indices), 2):
            value += self.residuals.get((a, b), 0.0)
        return value


def build_surrogate(
    evaluator: CounterfactualEvaluator,
    candidates: Sequence[Intervention],
    *,
    pairwise: bool = True,
    pairwise_cap: int = PAIRWISE_CANDIDATE_CAP,
) -> PairwiseSurrogate:
    """Measure singleton gains, and pairwise interactions when affordable.

    ``r_ij = g_i + g_j - g_ij`` is positive when two actions overlap - two
    injections on the same cash path do not help twice - and negative when they
    compound. A linear surrogate assumes it is always zero, which is precisely
    the assumption that makes a knapsack over singleton gains pick redundant
    sets.
    """
    baseline = evaluator.baseline_disruption()
    gains = [evaluator.marginal_gain([], u) for u in candidates]

    surrogate = PairwiseSurrogate(
        gains=gains, baseline=baseline, pairwise=pairwise and len(candidates) <= pairwise_cap
    )
    if not surrogate.pairwise:
        surrogate.simulations = evaluator.simulations_run
        return surrogate

    for i, j in itertools.combinations(range(len(candidates)), 2):
        joint = baseline - evaluator.disruption([candidates[i], candidates[j]])
        surrogate.residuals[(i, j)] = gains[i] + gains[j] - joint
    surrogate.simulations = evaluator.simulations_run
    return surrogate


def solve_milp(
    evaluator: CounterfactualEvaluator,
    candidates: Sequence[Intervention],
    graph: TemporalPaymentGraph,
    *,
    constraints: InterventionConstraints,
    objective: ObjectiveSpec = ObjectiveSpec(),
    time_limit_s: float = 10.0,
    pairwise: bool = True,
) -> SolverResult:
    """CP-SAT over the measured surrogate, then re-simulate the answer.

    The re-simulation is not a formality. The surrogate is a model of the
    simulator and models are wrong; reporting its objective would be reporting a
    prediction of a measurement that is already available.
    """
    started = time.perf_counter()
    if not candidates:
        baseline = evaluator.baseline_disruption()
        return SolverResult(
            method="cp_sat_pairwise",
            status="EMPTY",
            feasible=True,
            objective_value=objective.value(baseline, 0.0),
            disruption=baseline,
            baseline_disruption=baseline,
            runtime_s=time.perf_counter() - started,
            simulations=evaluator.simulations_run,
        )

    try:
        from ortools.sat.python import cp_model
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise DependencyUnavailableError(
            "the MILP solver needs the 'opt' extra: pip install -e '.[opt]'",
            missing=str(exc),
        ) from exc

    surrogate = build_surrogate(evaluator, candidates, pairwise=pairwise)
    baseline = surrogate.baseline
    epsilon = objective.resolve_epsilon(baseline)
    n = len(candidates)

    # CP-SAT is integral. Scales are derived from the data so resolution stays
    # meaningful on networks spanning several orders of magnitude in rupees.
    magnitude = max(
        [abs(g) for g in surrogate.gains] + [abs(r) for r in surrogate.residuals.values()] + [1.0]
    )
    gain_scale = 1_000_000.0 / magnitude
    cost_magnitude = max([u.cost for u in candidates] + [1.0])
    cost_scale = 1_000_000.0 / cost_magnitude

    model = cp_model.CpModel()
    x = [model.new_bool_var(f"x{i}") for i in range(n)]

    model.add(sum(x) <= constraints.max_actions)

    by_merchant: dict[str, list[int]] = {}
    for i, u in enumerate(candidates):
        by_merchant.setdefault(u.merchant_id, []).append(i)
    for indices in by_merchant.values():
        if len(indices) > constraints.max_per_merchant:
            model.add(sum(x[i] for i in indices) <= constraints.max_per_merchant)

    cost_terms = [round(u.cost * cost_scale) for u in candidates]
    if constraints.budget is not None:
        model.add(
            sum(cost_terms[i] * x[i] for i in range(n)) <= round(constraints.budget * cost_scale)
        )

    # Linearised products for the interaction terms.
    y: dict[tuple[int, int], Any] = {}
    for (i, j), residual in surrogate.residuals.items():
        if abs(residual) * gain_scale < 1.0:
            continue
        var = model.new_bool_var(f"y{i}_{j}")
        model.add(var <= x[i])
        model.add(var <= x[j])
        model.add(var >= x[i] + x[j] - 1)
        y[(i, j)] = var

    gain_expr = sum(round(surrogate.gains[i] * gain_scale) * x[i] for i in range(n)) - sum(
        round(surrogate.residuals[(i, j)] * gain_scale) * var for (i, j), var in y.items()
    )
    cost_expr = sum(cost_terms[i] * x[i] for i in range(n))

    if objective.form == "penalised":
        # min D + lambda*Cost is the same argmin as max gain - lambda*Cost:
        # the two differ only by the constant D(empty).
        model.maximize(
            gain_expr - _lam_term(objective.lam, cost_terms, x, gain_scale, cost_scale, n)
        )
    else:
        model.add(gain_expr >= round((baseline - epsilon) * gain_scale))
        model.minimize(cost_expr)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_search_workers = 1  # determinism over speed
    status = solver.Solve(model)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SolverResult(
            method="cp_sat_pairwise",
            status=status_name,
            feasible=False,
            baseline_disruption=baseline,
            runtime_s=time.perf_counter() - started,
            simulations=evaluator.simulations_run,
            n_candidates=n,
            notes={
                "reason": "solver found no feasible plan under the surrogate",
                "epsilon": epsilon if objective.form == "constrained" else None,
                "objective": objective.to_dict(),
            },
        )

    chosen = [candidates[i] for i in range(n) if solver.Value(x[i]) == 1]
    disruption = evaluator.disruption(chosen)
    cost = sum(u.cost for u in chosen)

    # The surrogate cannot express the liquidity floor - it is a property of the
    # trajectory, and the surrogate only knows disruption totals. So the chosen
    # set is checked after re-simulation, and a violation is *reported* rather
    # than hidden: a surrogate solver that cannot see a constraint should say so.
    feasibility = FeasibilityReport()
    if chosen:
        feasibility.violations.extend(check_action(chosen, graph, constraints).violations)
        feasibility.violations.extend(
            realised_floor_report(evaluator, chosen, constraints).violations
        )

    gap = None
    try:  # ObjectiveValue/BestObjectiveBound are only defined once an objective exists
        best_bound = solver.BestObjectiveBound()
        achieved = solver.ObjectiveValue()
        denominator = max(abs(achieved), 1.0)
        gap = abs(best_bound - achieved) / denominator
    except Exception:
        gap = None

    return SolverResult(
        interventions=chosen,
        method="cp_sat_pairwise" if surrogate.pairwise else "cp_sat_linear",
        status=status_name,
        feasible=feasibility.feasible,
        objective_value=objective.value(disruption, cost),
        disruption=disruption,
        baseline_disruption=baseline,
        cost=cost,
        gap=gap,
        runtime_s=time.perf_counter() - started,
        simulations=evaluator.simulations_run,
        n_candidates=n,
        feasibility=feasibility,
        notes={
            "surrogate": "pairwise" if surrogate.pairwise else "linear",
            "surrogate_predicted_disruption": surrogate.predict(
                [i for i in range(n) if solver.Value(x[i]) == 1]
            ),
            "surrogate_error": disruption
            - surrogate.predict([i for i in range(n) if solver.Value(x[i]) == 1]),
            "n_interaction_terms": len(y),
            "epsilon": epsilon if objective.form == "constrained" else None,
            "objective": objective.to_dict(),
            "note": "reported disruption is re-simulated; the surrogate is diagnostic only",
        },
    )


def _lam_term(
    lam: float,
    cost_terms: Sequence[int],
    x: Sequence[Any],
    gain_scale: float,
    cost_scale: float,
    n: int,
) -> Any:
    """``lambda * Cost`` expressed on the gain scale, as integers.

    Both sides of the penalised objective have to live on one scale before
    CP-SAT sees them, and the cost terms were already scaled by ``cost_scale``.
    """
    factor = lam * gain_scale / cost_scale
    return sum(round(cost_terms[i] * factor) * x[i] for i in range(n))
