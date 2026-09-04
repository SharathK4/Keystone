"""Robustness: choosing an action that survives being wrong about the shock.

A recommendation built on one point prediction is a bet on that prediction. The
contagion model is calibrated (Phase 3) but not certain, and the shock report
that triggers the decision is itself an estimate - an operator saying "a
five-lakh inflow has failed" is not measuring it to the rupee.

Deterministic worlds, not Monte Carlo
-------------------------------------
Full posterior propagation through a discrete-event simulator is not a laptop
computation and is not attempted. Instead a small, fixed, **seed-derived** family
of plausible worlds is constructed by perturbing the quantities that are
genuinely uncertain at decision time:

* shock magnitude - the reported size is an estimate;
* shock onset - the moment of failure is observed with a lag;
* settlement lag - rail latency varies.

The network structure and the obligation book are *not* perturbed: those are
observed, not inferred, and pretending otherwise would be manufacturing
uncertainty to make the robust mode look useful.

The objective
-------------
.. math::

    J_{\\text{robust}}(a) = \\operatorname{mean}_s D_s(a)
        + \\kappa \\operatorname{spread}_s D_s(a)
        + \\lambda \\, \\mathrm{Cost}(a)

``kappa = 0`` gives the expected-value objective exactly, so the robust mode is a
superset of the nominal one rather than a different system. ``spread`` is the
standard deviation by default and CVaR at the configured tail otherwise - CVaR
when the question is "how bad is the bad case", standard deviation when it is
"how much does this vary".

Because the worlds are deterministic given the seed, a robust decision is
exactly as reproducible as a nominal one.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from lce.config import ObjectiveSettings
from lce.domain.intervention import Intervention
from lce.domain.shock import Shock
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.intervention.exact import SolverResult
from lce.intervention.problem import (
    InterventionConstraints,
    ObjectiveSpec,
    check_action,
)
from lce.logging import get_logger
from lce.seeds import derive_seed
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import SimulationConfig

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UncertaintySpec:
    """Which quantities are treated as uncertain, and by how much."""

    n_scenarios: int = 5
    magnitude_spread: float = 0.25
    """Relative band on the reported shock magnitude: +/- 25% by default."""
    onset_spread_hours: float = 6.0
    settlement_lag_spread: float = 0.5
    """Relative band on rail latency."""
    kappa: float = 0.5
    """Penalty on dispersion. 0 gives plain expected disruption."""
    spread_measure: str = "std"
    """``std`` or ``cvar``."""
    cvar_alpha: float = 0.5
    """Tail fraction for CVaR: the mean of the worst ``alpha`` share of worlds."""
    seed: int = 20250101

    def __post_init__(self) -> None:
        if self.spread_measure not in ("std", "cvar"):
            raise ValueError(f"unknown spread measure {self.spread_measure!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_scenarios": self.n_scenarios,
            "magnitude_spread": self.magnitude_spread,
            "onset_spread_hours": self.onset_spread_hours,
            "settlement_lag_spread": self.settlement_lag_spread,
            "kappa": self.kappa,
            "spread_measure": self.spread_measure,
            "cvar_alpha": self.cvar_alpha,
            "seed": self.seed,
        }


@dataclass(slots=True)
class PerturbedWorld:
    """One plausible world: same network, differently-stated shock and rails."""

    name: str
    shock: Shock
    config: SimulationConfig
    magnitude_factor: float = 1.0
    onset_shift_hours: float = 0.0
    lag_factor: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "magnitude_factor": self.magnitude_factor,
            "onset_shift_hours": self.onset_shift_hours,
            "lag_factor": self.lag_factor,
            "shock_magnitude": self.shock.total_magnitude,
            "shock_onset_t": self.shock.onset_t,
        }


def build_worlds(
    shock: Shock, config: SimulationConfig, spec: UncertaintySpec
) -> list[PerturbedWorld]:
    """A deterministic family of worlds, the nominal one always first.

    Deterministic by construction: the factors come from a seeded generator keyed
    on the shock id, so the same decision problem always produces the same
    worlds, on any machine and in any process.
    """
    worlds = [PerturbedWorld(name="nominal", shock=shock, config=config)]
    if spec.n_scenarios <= 1:
        return worlds

    rng = np.random.default_rng(derive_seed(spec.seed, "robust", shock.shock_id))
    for index in range(spec.n_scenarios - 1):
        magnitude = float(
            np.clip(1.0 + rng.uniform(-spec.magnitude_spread, spec.magnitude_spread), 0.1, 5.0)
        )
        onset = float(rng.uniform(-spec.onset_spread_hours, spec.onset_spread_hours))
        drift = rng.uniform(-spec.settlement_lag_spread, spec.settlement_lag_spread)
        lag = float(np.clip(1.0 + drift, 0.1, 5.0))

        components = [
            component.model_copy(
                update={
                    "magnitude": max(component.magnitude * magnitude, 1e-6),
                    # Onset stays inside the horizon and non-negative: a shock
                    # that lands before t=0 or after the window is not a
                    # plausible world, it is a different problem.
                    "t": float(
                        np.clip(component.t + onset, 0.0, max(config.horizon_hours - 1.0, 0.0))
                    ),
                }
            )
            for component in shock.components
        ]
        worlds.append(
            PerturbedWorld(
                name=f"world_{index + 1}",
                shock=shock.model_copy(update={"components": components}),
                config=replace(
                    config, settlement_lag_hours=config.settlement_lag_hours * lag
                ),
                magnitude_factor=magnitude,
                onset_shift_hours=onset,
                lag_factor=lag,
            )
        )
    return worlds


def spread_of(values: np.ndarray, spec: UncertaintySpec) -> float:
    """Dispersion across worlds, by the configured measure."""
    if values.size < 2:
        return 0.0
    if spec.spread_measure == "std":
        return float(values.std(ddof=1))
    # CVaR: the mean of the worst alpha share, minus the mean, so the penalty is
    # zero for a plan that is uniformly average rather than merely uniform.
    k = max(1, round(spec.cvar_alpha * values.size))
    worst = np.sort(values)[-k:]
    return float(worst.mean() - values.mean())


@dataclass(slots=True)
class RobustEvaluation:
    """One action, scored across every world."""

    interventions: list[Intervention]
    per_world: dict[str, float] = field(default_factory=dict)
    mean_disruption: float = 0.0
    spread: float = 0.0
    cost: float = 0.0
    robust_value: float = 0.0
    nominal_disruption: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_actions": len(self.interventions),
            "interventions": [u.describe() for u in self.interventions],
            "per_world_disruption": dict(self.per_world),
            "mean_disruption": self.mean_disruption,
            "spread": self.spread,
            "nominal_disruption": self.nominal_disruption,
            "cost": self.cost,
            "robust_value": self.robust_value,
        }


@dataclass(slots=True)
class RobustResult:
    """The chosen action plus every alternative it was compared against."""

    chosen: RobustEvaluation
    alternatives: list[RobustEvaluation] = field(default_factory=list)
    worlds: list[dict[str, Any]] = field(default_factory=list)
    spec: UncertaintySpec = field(default_factory=UncertaintySpec)
    runtime_s: float = 0.0
    simulations: int = 0

    @property
    def nominal_choice_differs(self) -> bool:
        """Whether accounting for uncertainty changed the recommendation.

        The interesting flag. If it is always false, the robust mode is costing
        simulations and buying nothing on this benchmark, which is worth knowing
        and reporting rather than assuming.
        """
        if not self.alternatives:
            return False
        by_nominal = min(
            [self.chosen, *self.alternatives],
            key=lambda e: (e.nominal_disruption + e.cost, len(e.interventions)),
        )
        return {u.intervention_id for u in by_nominal.interventions} != {
            u.intervention_id for u in self.chosen.interventions
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen.to_dict(),
            "n_alternatives": len(self.alternatives),
            "alternatives": [a.to_dict() for a in self.alternatives],
            "worlds": self.worlds,
            "spec": self.spec.to_dict(),
            "nominal_choice_differs": self.nominal_choice_differs,
            "runtime_s": round(self.runtime_s, 4),
            "simulations": self.simulations,
        }


def robust_select(
    graph: TemporalPaymentGraph,
    plans: Sequence[Sequence[Intervention]],
    *,
    shock: Shock,
    config: SimulationConfig,
    constraints: InterventionConstraints,
    objective: ObjectiveSpec = ObjectiveSpec(),
    spec: UncertaintySpec = UncertaintySpec(),
    objective_settings: ObjectiveSettings | None = None,
) -> RobustResult:
    """Score every candidate plan in every world and take the robust argmin.

    ``plans`` is a small explicit list - typically the empty plan, each singleton,
    and whatever the nominal solver chose. Bounded by construction: the cost is
    ``|plans| x |worlds|`` simulations and both are capped by the resource
    profile, so robustness never turns into an unbounded search.
    """
    started = time.perf_counter()
    worlds = build_worlds(shock, config, spec)
    evaluators = [
        CounterfactualEvaluator(
            graph=graph, shock=world.shock, config=world.config, objective=objective_settings
        )
        for world in worlds
    ]

    evaluations: list[RobustEvaluation] = []
    for plan in plans:
        actions = list(plan)
        if actions and not check_action(actions, graph, constraints).feasible:
            continue
        per_world = {
            world.name: evaluator.disruption(actions)
            for world, evaluator in zip(worlds, evaluators, strict=True)
        }
        values = np.array(list(per_world.values()), dtype=float)
        cost = sum(u.cost for u in actions)
        mean = float(values.mean())
        spread = spread_of(values, spec)
        evaluations.append(
            RobustEvaluation(
                interventions=actions,
                per_world=per_world,
                mean_disruption=mean,
                spread=spread,
                cost=cost,
                robust_value=mean + spec.kappa * spread + objective.lam * cost,
                nominal_disruption=per_world["nominal"],
            )
        )

    if not evaluations:
        raise ValueError("no feasible plan was supplied to the robust selector")

    evaluations.sort(
        key=lambda e: (e.robust_value, e.cost, len(e.interventions))
    )
    chosen = evaluations[0]

    result = RobustResult(
        chosen=chosen,
        alternatives=evaluations[1:],
        worlds=[w.to_dict() for w in worlds],
        spec=spec,
        runtime_s=time.perf_counter() - started,
        simulations=sum(e.simulations_run for e in evaluators),
    )
    logger.info(
        "robust_selection",
        n_plans=len(evaluations),
        n_worlds=len(worlds),
        kappa=spec.kappa,
        choice_differs=result.nominal_choice_differs,
    )
    return result


def plans_from_solver(
    solver: SolverResult, candidates: Sequence[Intervention], *, max_singletons: int = 8
) -> list[list[Intervention]]:
    """The plan set the robust selector compares: nothing, singletons, the solve.

    Deliberately small. The robust question is "is the nominal choice fragile?",
    which needs a handful of serious alternatives, not a re-run of the whole
    search under every world.
    """
    plans: list[list[Intervention]] = [[]]
    plans.extend([[u] for u in candidates[:max_singletons]])
    if solver.interventions and solver.interventions not in plans:
        plans.append(list(solver.interventions))
    return plans
