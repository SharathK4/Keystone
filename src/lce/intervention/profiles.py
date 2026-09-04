"""Resource profiles - the laptop budget, made explicit.

Every expensive knob in Phase 4 is bounded by one of three named profiles rather
than by whatever the caller happened to pass. The point is that a run either
fits the budget or is refused; there is no configuration in which the optimiser
quietly starts a computation that will not finish on a developer machine.

| profile      | network | candidates | actions | solver | exact optimum |
|--------------|---------|-----------|---------|--------|---------------|
| SMALL_FAST   | 100     | 12        | 2       | 5s     | yes           |
| MEDIUM       | 1,000   | 24        | 3       | 15s    | no            |
| LARGE_DEMO   | 10,000  | 32        | 3       | 30s    | no            |

The simulation counts these imply are what actually determine runtime:

``SMALL_FAST``  ``sum_{j<=2} C(12,j) = 79`` subsets, so the exact optimum costs
                79 simulations and is genuinely affordable.
``MEDIUM``      the pairwise surrogate costs ``24 + 276 = 300`` simulations;
                greedy costs ``3 * 24 = 72``.
``LARGE_DEMO``  greedy only. The surrogate is disabled: at 10,000 merchants a
                single simulation is expensive enough that ``O(n^2)`` of them is
                not a laptop computation, and pretending otherwise would be
                faking scalability rather than reporting its boundary.

``LARGE_DEMO`` demonstrates that the pipeline runs at scale. It does not
demonstrate optimality at scale, because no exact optimum is computed there -
that limit is stated in the documentation rather than hidden behind a default.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from lce.intervention.problem import InterventionConstraints


class ResourceProfile(StrEnum):
    SMALL_FAST = "small_fast"
    MEDIUM = "medium"
    LARGE_DEMO = "large_demo"


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Hard limits for one profile."""

    profile: ResourceProfile
    scale: str
    """Benchmark scale name. Deliberately a plain string rather than the
    ``BenchmarkScale`` enum: importing it would pull the benchmark package - and
    with it the dataset generator - into every process that reads a resource
    profile, including the production inference service."""
    max_candidates: int
    max_actions: int
    solver_time_limit_s: float
    exact_optimum: bool
    pairwise_surrogate: bool
    n_robust_scenarios: int
    horizon_hours: float = 168.0
    systemic_sample: int | None = None
    """How many merchants the systemic sweep covers. ``None`` means all of them;
    at MEDIUM and above the sweep is one simulation per merchant and has to be
    sampled or it dominates the entire run."""
    approx_peak_memory_mb: int = 2048
    """Advisory, measured rather than enforced - reported next to the result so a
    profile that outgrows a 16 GB machine is visible."""

    def constraints(self, **overrides: Any) -> InterventionConstraints:
        """The feasible set implied by this profile."""
        base = InterventionConstraints(
            max_actions=self.max_actions, horizon_hours=self.horizon_hours
        )
        return replace(base, **overrides) if overrides else base

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": str(self.profile),
            "scale": self.scale,
            "max_candidates": self.max_candidates,
            "max_actions": self.max_actions,
            "solver_time_limit_s": self.solver_time_limit_s,
            "exact_optimum": self.exact_optimum,
            "pairwise_surrogate": self.pairwise_surrogate,
            "n_robust_scenarios": self.n_robust_scenarios,
            "horizon_hours": self.horizon_hours,
            "systemic_sample": self.systemic_sample,
            "approx_peak_memory_mb": self.approx_peak_memory_mb,
        }

    @property
    def exact_subset_count(self) -> int:
        """Subsets a complete enumeration would visit under this profile."""
        from math import comb

        k = min(self.max_actions, self.max_candidates)
        return sum(comb(self.max_candidates, j) for j in range(0, k + 1))


BUDGETS: dict[ResourceProfile, ResourceBudget] = {
    ResourceProfile.SMALL_FAST: ResourceBudget(
        profile=ResourceProfile.SMALL_FAST,
        scale="small",
        max_candidates=12,
        max_actions=2,
        solver_time_limit_s=5.0,
        exact_optimum=True,
        pairwise_surrogate=True,
        n_robust_scenarios=5,
        systemic_sample=40,
        approx_peak_memory_mb=1024,
    ),
    ResourceProfile.MEDIUM: ResourceBudget(
        profile=ResourceProfile.MEDIUM,
        scale="medium",
        max_candidates=24,
        max_actions=3,
        solver_time_limit_s=15.0,
        exact_optimum=False,
        pairwise_surrogate=True,
        n_robust_scenarios=5,
        systemic_sample=60,
        approx_peak_memory_mb=3072,
    ),
    ResourceProfile.LARGE_DEMO: ResourceBudget(
        profile=ResourceProfile.LARGE_DEMO,
        scale="large",
        max_candidates=32,
        max_actions=3,
        solver_time_limit_s=30.0,
        exact_optimum=False,
        # Disabled on purpose: O(n^2) simulations at 10,000 merchants is not a
        # laptop computation. Greedy only, and the boundary is documented.
        pairwise_surrogate=False,
        n_robust_scenarios=3,
        systemic_sample=25,
        approx_peak_memory_mb=6144,
    ),
}


def budget_for(profile: ResourceProfile | str) -> ResourceBudget:
    return BUDGETS[ResourceProfile(profile)]


def estimate_simulations(budget: ResourceBudget, *, method: str) -> int:
    """Simulations a method will run under a profile, before it is started.

    Used by the runner to refuse a configuration up front rather than after
    twenty minutes of work.
    """
    n = budget.max_candidates
    match method:
        case "exact":
            return budget.exact_subset_count
        case "milp":
            return n + (n * (n - 1) // 2 if budget.pairwise_surrogate else 0) + 1
        case "greedy":
            return budget.max_actions * n + 1
        case "baseline":
            return 2
    raise ValueError(f"unknown method {method!r}")
