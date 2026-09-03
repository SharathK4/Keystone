"""Intervention search.

The problem is

.. math::

    \\min_{U \\subseteq \\mathcal{U}} D(G, S, U)
    \\quad\\text{s.t.}\\quad \\sum_{u \\in U} c(u) \\le B,\\;\\; |U| \\le k

where every evaluation of :math:`D` is a full counterfactual simulation. That
shape - expensive black-box objective, small cardinality budget - rules out
gradient methods and rules in search. Three strategies are provided, and they
exist to be compared:

``GreedySearch``
    Repeatedly adds the action with the best marginal *disruption prevented per
    rupee*, re-simulating each time. Optionally uses lazy (CELF) evaluation.

``CpSatSearch``
    Precomputes singleton gains, then solves an exact knapsack over that
    **linear surrogate** with OR-Tools CP-SAT, and re-simulates the chosen set to
    get its true value. Fast and globally optimal *for the surrogate*; the
    surrogate ignores interactions between actions, which is exactly the
    trade-off the evaluation surfaces.

``ExhaustiveSearch``
    Enumerates every feasible subset up to size ``k``. Exponential, so it is
    guarded by a hard cap - but on small instances it yields the true optimum
    :math:`U^*`, which is what makes the reported **optimality gap** a real
    measurement rather than a comparison against another heuristic.

A note on lazy greedy
---------------------
CELF's correctness argument requires the objective to be submodular. Disruption
prevented is *not* provably submodular here: two injections upstream and
downstream of the same link can interact super-additively. Lazy evaluation is
therefore a genuine approximation, not a free speed-up, and it is off by default.
The optimality gap against ``ExhaustiveSearch`` is what tells you whether it cost
you anything on a given network.
"""

from __future__ import annotations

import heapq
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from lce.domain.enums import OptimizerKind
from lce.domain.intervention import Intervention, InterventionPlan
from lce.errors import DependencyUnavailableError, OptimizationError
from lce.logging import get_logger
from lce.simulation.counterfactual import CounterfactualEvaluator

logger = get_logger(__name__)

# Refuse to enumerate more than this many subsets; exhaustive search is only
# meant for measuring the gap on small instances.
EXHAUSTIVE_SUBSET_CAP = 200_000


@dataclass(frozen=True, slots=True)
class SearchConfig:
    """Budget and cardinality constraints, plus search behaviour."""

    budget: float | None = None
    max_actions: int = 3
    lazy: bool = False
    min_gain: float = 0.0
    one_per_merchant: bool = True
    cp_sat_time_limit_s: float = 10.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "max_actions": self.max_actions,
            "lazy": self.lazy,
            "min_gain": self.min_gain,
            "one_per_merchant": self.one_per_merchant,
            "cp_sat_time_limit_s": self.cp_sat_time_limit_s,
        }


@dataclass(slots=True)
class SearchResult:
    """A chosen plan plus the search telemetry needed to judge it."""

    plan: InterventionPlan
    optimizer: OptimizerKind
    baseline_disruption: float
    achieved_disruption: float
    candidates_considered: int
    simulations_run: int
    elapsed_ms: float
    ranked: list[tuple[Intervention, float, float]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def disruption_prevented(self) -> float:
        return self.baseline_disruption - self.achieved_disruption

    @property
    def cost(self) -> float:
        return self.plan.total_cost

    @property
    def disruption_prevented_per_rupee(self) -> float:
        if self.cost <= 0:
            return float("inf") if self.disruption_prevented > 0 else 0.0
        return self.disruption_prevented / self.cost

    def summary(self) -> dict[str, Any]:
        return {
            "optimizer": str(self.optimizer),
            "n_actions": len(self.plan.interventions),
            "cost": self.cost,
            "baseline_disruption": self.baseline_disruption,
            "achieved_disruption": self.achieved_disruption,
            "disruption_prevented": self.disruption_prevented,
            "disruption_prevented_per_rupee": self.disruption_prevented_per_rupee,
            "candidates_considered": self.candidates_considered,
            "simulations_run": self.simulations_run,
            "elapsed_ms": self.elapsed_ms,
        }


class Search(Protocol):
    """Common interface so strategies can be swapped in the evaluation."""

    kind: OptimizerKind

    def run(
        self,
        evaluator: CounterfactualEvaluator,
        candidates: list[Intervention],
        config: SearchConfig,
    ) -> SearchResult: ...


def _feasible(
    chosen: list[Intervention], candidate: Intervention, config: SearchConfig
) -> bool:
    if config.budget is not None:
        total = sum(u.cost for u in chosen) + candidate.cost
        if total > config.budget + 1e-6:
            return False
    return not (
        config.one_per_merchant
        and any(u.merchant_id == candidate.merchant_id for u in chosen)
    )


def _finish(
    evaluator: CounterfactualEvaluator,
    chosen: list[Intervention],
    optimizer: OptimizerKind,
    config: SearchConfig,
    candidates_considered: int,
    started: float,
    ranked: list[tuple[Intervention, float, float]] | None = None,
    notes: dict[str, Any] | None = None,
) -> SearchResult:
    baseline = evaluator.baseline_disruption()
    achieved = evaluator.disruption(chosen)
    plan = InterventionPlan(
        interventions=list(chosen),
        budget=config.budget,
        max_actions=config.max_actions,
        optimizer=str(optimizer),
    ).with_evaluation(baseline, achieved, str(optimizer))
    return SearchResult(
        plan=plan,
        optimizer=optimizer,
        baseline_disruption=baseline,
        achieved_disruption=achieved,
        candidates_considered=candidates_considered,
        simulations_run=evaluator.simulations_run,
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        ranked=ranked or [],
        notes=notes or {},
    )


class GreedySearch:
    """Marginal disruption-prevented-per-rupee, one action at a time."""

    kind = OptimizerKind.GREEDY

    def run(
        self,
        evaluator: CounterfactualEvaluator,
        candidates: list[Intervention],
        config: SearchConfig,
    ) -> SearchResult:
        started = time.perf_counter()
        if not candidates:
            return _finish(evaluator, [], self.kind, config, 0, started)

        chosen: list[Intervention] = []
        remaining = list(candidates)
        ranked: list[tuple[Intervention, float, float]] = []

        for _ in range(config.max_actions):
            pool = [u for u in remaining if _feasible(chosen, u, config)]
            if not pool:
                break
            best = (
                self._lazy_pick(evaluator, chosen, pool, config)
                if config.lazy
                else self._eager_pick(evaluator, chosen, pool, config)
            )
            if best is None:
                break
            candidate, gain, ratio = best
            chosen.append(candidate)
            remaining.remove(candidate)
            ranked.append((candidate, gain, ratio))

        return _finish(
            evaluator,
            chosen,
            self.kind,
            config,
            len(candidates),
            started,
            ranked,
            {"lazy": config.lazy},
        )

    def _score(self, gain: float, candidate: Intervention) -> float:
        """Disruption prevented per rupee; free-but-effective ranks highest."""
        if candidate.cost <= 0:
            return float("inf") if gain > 0 else 0.0
        return gain / candidate.cost

    def _eager_pick(
        self,
        evaluator: CounterfactualEvaluator,
        chosen: list[Intervention],
        pool: list[Intervention],
        config: SearchConfig,
    ) -> tuple[Intervention, float, float] | None:
        best: tuple[Intervention, float, float] | None = None
        for candidate in pool:
            gain = evaluator.marginal_gain(chosen, candidate)
            if gain <= config.min_gain:
                continue
            ratio = self._score(gain, candidate)
            if best is None or ratio > best[2]:
                best = (candidate, gain, ratio)
        return best

    def _lazy_pick(
        self,
        evaluator: CounterfactualEvaluator,
        chosen: list[Intervention],
        pool: list[Intervention],
        config: SearchConfig,
    ) -> tuple[Intervention, float, float] | None:
        """CELF-style lazy evaluation. See the module docstring's caveat."""
        heap: list[tuple[float, int, Intervention, int]] = []
        for i, candidate in enumerate(pool):
            gain = evaluator.marginal_gain(chosen, candidate)
            heapq.heappush(heap, (-self._score(gain, candidate), i, candidate, len(chosen)))

        while heap:
            neg_ratio, i, candidate, stamp = heapq.heappop(heap)
            if stamp == len(chosen):
                gain = evaluator.marginal_gain(chosen, candidate)
                if gain <= config.min_gain:
                    continue
                return candidate, gain, -neg_ratio
            gain = evaluator.marginal_gain(chosen, candidate)
            heapq.heappush(heap, (-self._score(gain, candidate), i, candidate, len(chosen)))
        return None


class CpSatSearch:
    """Exact knapsack over singleton gains, solved with OR-Tools CP-SAT."""

    kind = OptimizerKind.CP_SAT_KNAPSACK

    def run(
        self,
        evaluator: CounterfactualEvaluator,
        candidates: list[Intervention],
        config: SearchConfig,
    ) -> SearchResult:
        started = time.perf_counter()
        if not candidates:
            return _finish(evaluator, [], self.kind, config, 0, started)

        try:
            from ortools.sat.python import cp_model
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise DependencyUnavailableError(
                "CP-SAT search needs the 'opt' extra. Install it with: "
                "pip install -e '.[opt]'",
                missing=str(exc),
            ) from exc

        # Linear surrogate: the marginal value of each action *alone*.
        gains = [evaluator.marginal_gain([], u) for u in candidates]

        # CP-SAT is integral, so gains and costs are scaled to integers. The
        # scale is derived from the data rather than fixed, to keep resolution
        # meaningful across networks spanning many orders of magnitude.
        max_gain = max((g for g in gains if g > 0), default=0.0)
        if max_gain <= 0:
            return _finish(
                evaluator, [], self.kind, config, len(candidates), started,
                notes={"reason": "no candidate had positive singleton gain"},
            )
        gain_scale = 1_000_000.0 / max_gain
        cost_scale = 1000.0 / max(max((u.cost for u in candidates), default=1.0), 1.0)

        model = cp_model.CpModel()
        x = [model.new_bool_var(f"u{i}") for i in range(len(candidates))]

        model.add(sum(x) <= config.max_actions)
        if config.budget is not None:
            model.add(
                sum(
                    round(u.cost * cost_scale) * x[i]
                    for i, u in enumerate(candidates)
                )
                <= round(config.budget * cost_scale)
            )
        if config.one_per_merchant:
            by_merchant: dict[str, list[int]] = {}
            for i, u in enumerate(candidates):
                by_merchant.setdefault(u.merchant_id, []).append(i)
            for indices in by_merchant.values():
                if len(indices) > 1:
                    model.add(sum(x[i] for i in indices) <= 1)

        model.maximize(
            sum(round(max(0.0, gains[i]) * gain_scale) * x[i] for i in range(len(x)))
        )

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = config.cp_sat_time_limit_s
        solver.parameters.num_search_workers = 1  # determinism over speed
        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            raise OptimizationError(
                "CP-SAT found no feasible intervention plan",
                status=solver.StatusName(status),
            )

        chosen = [candidates[i] for i in range(len(x)) if solver.Value(x[i]) == 1]
        ranked = [
            (candidates[i], gains[i], gains[i] / candidates[i].cost if candidates[i].cost else 0.0)
            for i in range(len(x))
            if solver.Value(x[i]) == 1
        ]
        return _finish(
            evaluator,
            chosen,
            self.kind,
            config,
            len(candidates),
            started,
            ranked,
            {
                "cp_sat_status": solver.StatusName(status),
                "surrogate_objective": solver.ObjectiveValue() / gain_scale,
                "note": "objective is the linear surrogate; achieved_disruption is re-simulated",
            },
        )


class ExhaustiveSearch:
    """Brute force over all feasible subsets - the reference optimum."""

    kind = OptimizerKind.EXHAUSTIVE

    def run(
        self,
        evaluator: CounterfactualEvaluator,
        candidates: list[Intervention],
        config: SearchConfig,
    ) -> SearchResult:
        started = time.perf_counter()
        if not candidates:
            return _finish(evaluator, [], self.kind, config, 0, started)

        n = len(candidates)
        k = min(config.max_actions, n)
        total = sum(_n_choose_k(n, size) for size in range(1, k + 1))
        if total > EXHAUSTIVE_SUBSET_CAP:
            raise OptimizationError(
                f"exhaustive search would evaluate {total:,} subsets "
                f"(cap {EXHAUSTIVE_SUBSET_CAP:,}); reduce the candidate set or max_actions",
                n_candidates=n,
                max_actions=k,
            )

        best_subset: list[Intervention] = []
        best_disruption = evaluator.baseline_disruption()

        for size in range(1, k + 1):
            for subset in itertools.combinations(candidates, size):
                chosen: list[Intervention] = []
                feasible = True
                for candidate in subset:
                    if not _feasible(chosen, candidate, config):
                        feasible = False
                        break
                    chosen.append(candidate)
                if not feasible:
                    continue
                disruption = evaluator.disruption(chosen)
                if disruption < best_disruption - 1e-9:
                    best_disruption = disruption
                    best_subset = chosen

        return _finish(
            evaluator,
            best_subset,
            self.kind,
            config,
            len(candidates),
            started,
            notes={"subsets_enumerated": total},
        )


class TopExposureSearch:
    """Naive control: spend the budget on the highest-exposure nodes.

    Included so the reported numbers have a floor to beat. An optimiser that
    cannot outperform "give money to whoever looks worst" is not earning its
    simulation budget.
    """

    kind = OptimizerKind.TOP_EXPOSURE

    def run(
        self,
        evaluator: CounterfactualEvaluator,
        candidates: list[Intervention],
        config: SearchConfig,
    ) -> SearchResult:
        started = time.perf_counter()
        chosen: list[Intervention] = []
        # Candidates arrive ordered by the predictor's ranking of their node.
        for candidate in candidates:
            if len(chosen) >= config.max_actions:
                break
            if _feasible(chosen, candidate, config):
                chosen.append(candidate)
        return _finish(evaluator, chosen, self.kind, config, len(candidates), started)


def _n_choose_k(n: int, k: int) -> int:
    from math import comb

    return comb(n, k)


SEARCH_REGISTRY: dict[OptimizerKind, type] = {
    OptimizerKind.GREEDY: GreedySearch,
    OptimizerKind.CP_SAT_KNAPSACK: CpSatSearch,
    OptimizerKind.EXHAUSTIVE: ExhaustiveSearch,
    OptimizerKind.TOP_EXPOSURE: TopExposureSearch,
}


def build_search(kind: OptimizerKind) -> Any:
    """Instantiate a search strategy by name."""
    try:
        return SEARCH_REGISTRY[kind]()
    except KeyError as exc:
        raise OptimizationError(f"unknown optimizer {kind!r}") from exc
