"""Scientific validation and distributional diagnostics.

Two distinct jobs, kept separate because they fail differently:

**Validation** asserts properties that must hold or the benchmark is invalid -
flow consistency, monotonicity in shock magnitude, conservation under
intervention. A failure here means results computed from this dataset are
meaningless, not merely unusual.

**Diagnostics** describe the data without judging it - degree distributions,
amount tails, cycle and bottleneck counts, seasonality strength. These exist so
a pathological dataset (every node identical, no cycles, a single payment
carrying 90% of the volume) is visible rather than silently averaged into a
headline number.

The monotonicity checks are the ones that have actually caught bugs here: an
objective that is not monotone in shock magnitude means the disruption measure
is broken, and every optimiser result computed against it is noise.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import numpy as np

from lce.benchmark.scenarios import (
    ScenarioFamily,
    ScenarioSpec,
    TargetStrategy,
    build_scenario,
)
from lce.config import ObjectiveSettings
from lce.data.generator import SyntheticNetwork
from lce.domain.events import EXTERNAL_SINK
from lce.domain.intervention import Intervention, InterventionPlan, InterventionType
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.simulation.engine import LiquiditySimulator, SimulationConfig

logger = get_logger(__name__)


@dataclass(slots=True)
class Check:
    """One validation result."""

    name: str
    passed: bool
    detail: str = ""
    severity: str = "error"  # error | warning
    values: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "detail": self.detail,
            "values": self.values,
        }


@dataclass(slots=True)
class ValidationReport:
    """The outcome of validating one dataset."""

    dataset_id: str
    checks: list[Check] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True when no *error*-severity check failed."""
        return not any(not c.passed and c.severity == "error" for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "warning"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "passed": self.passed,
            "n_checks": len(self.checks),
            "n_failures": len(self.failures),
            "n_warnings": len(self.warnings),
            "checks": [c.to_dict() for c in self.checks],
            "diagnostics": self.diagnostics,
        }

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"{status}  {len(self.checks) - len(self.failures) - len(self.warnings)}"
            f"/{len(self.checks)} checks clean, "
            f"{len(self.failures)} failures, {len(self.warnings)} warnings"
        )


# --------------------------------------------------------------- diagnostics


def _gini(values: np.ndarray) -> float:
    """Gini coefficient - 0 is uniform, 1 is maximally concentrated."""
    if values.size == 0:
        return 0.0
    sorted_values = np.sort(np.clip(values.astype(float), 0.0, None))
    total = sorted_values.sum()
    if total <= 0:
        return 0.0
    n = sorted_values.size
    index = np.arange(1, n + 1)
    return float((2.0 * (index * sorted_values).sum()) / (n * total) - (n + 1.0) / n)


def _tail_index(amounts: np.ndarray) -> float | None:
    """Hill estimator on the top decile - lower means a heavier tail."""
    positive = amounts[amounts > 0]
    if positive.size < 50:
        return None
    ordered = np.sort(positive)[::-1]
    k = max(10, int(0.1 * ordered.size))
    k = min(k, ordered.size - 1)
    top = ordered[:k]
    threshold = ordered[k]
    if threshold <= 0:
        return None
    logs = np.log(top / threshold)
    mean_log = float(logs.mean())
    return 1.0 / mean_log if mean_log > 1e-12 else None


def compute_diagnostics(network: SyntheticNetwork) -> dict[str, Any]:
    """Distributional and structural description of a generated network."""
    graph = network.graph
    stats = graph.stats()

    amounts = np.array([e.amount for e in graph.payment_events], dtype=float)
    obligations = np.array(
        [o.amount for o in graph.obligations if o.creditor_id != EXTERNAL_SINK],
        dtype=float,
    )
    buffers = np.array(
        [graph.merchant(m).initial_buffer for m in graph.merchant_ids], dtype=float
    )

    out_degree = Counter(s for s, _ in network.ground_truth_edges)
    in_degree = Counter(t for _, t in network.ground_truth_edges)
    in_counts = np.array(
        [in_degree.get(m, 0) for m in graph.merchant_ids], dtype=float
    )

    dg = graph.dependency_graph()
    try:
        n_cycles = len(list(nx.simple_cycles(dg, length_bound=6)))
    except TypeError:  # pragma: no cover - older networkx without length_bound
        n_cycles = sum(1 for _ in zip(nx.simple_cycles(dg), range(500), strict=False))

    # A "bottleneck" is a node an outsized share of the network sits behind.
    reach = {m: len(graph.descendants_within(m, 4)) for m in graph.merchant_ids}
    reach_values = np.array(list(reach.values()), dtype=float)
    bottleneck_threshold = max(3.0, 0.10 * len(graph))
    bottlenecks = sorted(
        (m for m, r in reach.items() if r >= bottleneck_threshold),
        key=lambda m: -reach[m],
    )

    # Seasonality strength: coefficient of variation of exogenous inflow volume
    # bucketed by hour-of-week.
    weekly = np.zeros(7, dtype=float)
    for event in graph.payment_events:
        if event.metadata.get("driver") == "exogenous":
            weekly[int((event.t % 168.0) // 24.0)] += event.amount
    seasonality_cv = (
        float(weekly.std() / weekly.mean()) if weekly.mean() > 0 else 0.0
    )

    sector_counts = Counter(
        str(graph.merchant(m).sector) for m in graph.merchant_ids
    )
    same_sector_edges = sum(
        1
        for s, t in network.ground_truth_edges
        if graph.merchant(s).sector == graph.merchant(t).sector
    )

    return {
        "n_merchants": stats.n_merchants,
        "n_payment_events": stats.n_payment_events,
        "n_obligations": stats.n_obligations,
        "n_dependency_edges": stats.n_dependency_edges,
        "events_per_edge": stats.n_payment_events / max(1, stats.n_dependency_edges),
        "amount": _describe(amounts),
        "amount_tail_index_hill": _tail_index(amounts),
        "amount_gini": _gini(amounts),
        "obligation_amount": _describe(obligations),
        "buffer": _describe(buffers),
        "buffer_gini": _gini(buffers),
        "in_degree": {
            "max": int(in_counts.max()) if in_counts.size else 0,
            "mean": float(in_counts.mean()) if in_counts.size else 0.0,
            "gini": _gini(in_counts),
        },
        "out_degree_max": max(out_degree.values()) if out_degree else 0,
        "n_cycles_up_to_len6": n_cycles,
        "max_downstream_reach": int(reach_values.max()) if reach_values.size else 0,
        "mean_downstream_reach": float(reach_values.mean()) if reach_values.size else 0.0,
        "n_bottlenecks": len(bottlenecks),
        "top_bottlenecks": bottlenecks[:5],
        "seasonality_cv": seasonality_cv,
        "n_sectors": len(sector_counts),
        "sector_counts": dict(sector_counts),
        "same_sector_edge_share": (
            same_sector_edges / len(network.ground_truth_edges)
            if network.ground_truth_edges
            else 0.0
        ),
        "layer_sizes": {
            str(layer): sum(1 for v in network.layers.values() if v == layer)
            for layer in sorted(set(network.layers.values()))
        },
    }


def _describe(values: np.ndarray) -> dict[str, float]:
    if values.size == 0:
        return {"n": 0.0}
    return {
        "n": float(values.size),
        "mean": float(values.mean()),
        "std": float(values.std()),
        "min": float(values.min()),
        "p50": float(np.percentile(values, 50)),
        "p95": float(np.percentile(values, 95)),
        "p99": float(np.percentile(values, 99)),
        "max": float(values.max()),
        "max_over_p50": float(values.max() / np.percentile(values, 50))
        if np.percentile(values, 50) > 0
        else 0.0,
    }


# ---------------------------------------------------------------- structural


def check_flow_consistency(network: SyntheticNetwork) -> Check:
    """No merchant may owe more than its throughput net of retained margin.

    This is the property that makes contagion measurable at all: violate it and
    the network defaults with no shock applied, so there is no signal left to
    attribute to one.
    """
    graph = network.graph
    worst = 0.0
    offender = ""
    checked = 0
    for merchant_id in graph.merchant_ids:
        out_edges = graph.out_dependencies(merchant_id)
        throughput = network.throughput.get(merchant_id, 0.0)
        if not out_edges or throughput <= 0:
            continue
        allocated = sum(float(e.metadata.get("horizon_flow", 0.0)) for e in out_edges)
        margin = float(graph.merchant(merchant_id).metadata.get("margin", 0.0))
        budget = throughput * (1.0 - margin)
        if budget <= 0:
            continue
        excess = (allocated - budget) / budget
        checked += 1
        if excess > worst:
            worst, offender = excess, merchant_id

    return Check(
        name="flow_consistency",
        passed=worst <= 1e-6,
        detail=(
            f"checked {checked} merchants; worst over-allocation "
            f"{worst:.2%}" + (f" at {offender}" if offender else "")
        ),
        values={"worst_excess": worst, "n_checked": checked},
    )


def check_no_negative_opening_balances(network: SyntheticNetwork) -> Check:
    """Opening balances and floors must be non-negative and internally coherent."""
    bad = []
    for merchant_id in network.graph.merchant_ids:
        profile = network.graph.merchant(merchant_id)
        if profile.opening_balance < 0 or profile.credit_limit < 0 or profile.initial_buffer < 0:
            bad.append(merchant_id)
    return Check(
        name="no_negative_opening_balances",
        passed=not bad,
        detail=f"{len(bad)} merchants with negative balance or buffer",
        values={"offenders": bad[:10]},
    )


def check_structural_variation(
    network: SyntheticNetwork, diagnostics: dict[str, Any]
) -> list[Check]:
    """The network must contain real chains, cycles and bottlenecks.

    A generator that emits a flat bipartite graph would pass every numeric check
    while being useless as a contagion benchmark: with no depth there is nothing
    for a cascade to travel along.
    """
    checks = [
        Check(
            name="has_chains",
            passed=diagnostics["max_downstream_reach"] >= 2,
            detail=(
                f"deepest downstream reach is "
                f"{diagnostics['max_downstream_reach']} nodes"
            ),
            values={"max_downstream_reach": diagnostics["max_downstream_reach"]},
        ),
        Check(
            name="has_bottlenecks",
            passed=diagnostics["n_bottlenecks"] >= 1,
            severity="warning",
            detail=f"{diagnostics['n_bottlenecks']} nodes with outsized downstream reach",
            values={"n_bottlenecks": diagnostics["n_bottlenecks"]},
        ),
        Check(
            name="has_cycles",
            passed=diagnostics["n_cycles_up_to_len6"] >= 1,
            severity="warning",
            detail=(
                f"{diagnostics['n_cycles_up_to_len6']} cycles of length <= 6; "
                "back-edges make the graph non-trivially cyclic"
            ),
            values={"n_cycles": diagnostics["n_cycles_up_to_len6"]},
        ),
        Check(
            name="heterogeneous_merchant_sizes",
            passed=diagnostics["buffer_gini"] >= 0.20,
            detail=f"buffer Gini {diagnostics['buffer_gini']:.3f}",
            values={"buffer_gini": diagnostics["buffer_gini"]},
        ),
        Check(
            name="heavy_tailed_amounts",
            passed=diagnostics["amount"].get("max_over_p50", 0.0) >= 20.0,
            detail=(
                f"max/p50 = {diagnostics['amount'].get('max_over_p50', 0.0):.0f}, "
                f"Hill index {diagnostics['amount_tail_index_hill']}"
            ),
            values={
                "max_over_p50": diagnostics["amount"].get("max_over_p50", 0.0),
                "hill_index": diagnostics["amount_tail_index_hill"],
            },
        ),
        Check(
            name="amounts_not_degenerate",
            passed=diagnostics["amount_gini"] < 0.999,
            detail=(
                f"amount Gini {diagnostics['amount_gini']:.4f}; a value at 1.0 means "
                "a single payment carries the whole volume"
            ),
            values={"amount_gini": diagnostics["amount_gini"]},
        ),
        Check(
            name="seasonality_present",
            passed=diagnostics["seasonality_cv"] > 0.02,
            severity="warning",
            detail=f"weekly volume CV {diagnostics['seasonality_cv']:.3f}",
            values={"seasonality_cv": diagnostics["seasonality_cv"]},
        ),
        Check(
            name="sector_clustering",
            passed=diagnostics["n_sectors"] >= 2,
            severity="warning",
            detail=(
                f"{diagnostics['n_sectors']} sectors present, "
                f"{diagnostics['same_sector_edge_share']:.2%} of edges intra-sector"
            ),
            values={
                "n_sectors": diagnostics["n_sectors"],
                "same_sector_edge_share": diagnostics["same_sector_edge_share"],
            },
        ),
        Check(
            name="sufficient_events_per_edge",
            passed=diagnostics["events_per_edge"] >= 20.0,
            severity="warning",
            detail=(
                f"{diagnostics['events_per_edge']:.1f} events per edge; below ~20 the "
                "dependency learner cannot separate excitation from baseline"
            ),
            values={"events_per_edge": diagnostics["events_per_edge"]},
        ),
    ]
    return checks


# ----------------------------------------------------------------- dynamical


def check_shock_monotonicity(
    network: SyntheticNetwork,
    *,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
    magnitudes: tuple[float, ...] = (1.0, 2.0, 4.0),
    tolerance: float = 0.05,
) -> list[Check]:
    """A shock must not shrink the damage, and a bigger one must not help.

    Two properties, asserted at the strength each actually holds.

    **The affected set grows on net.** A shock must leave at least as many
    merchants damaged as the baseline did, and must newly damage at least as
    many as it incidentally spares. Exact set *inclusion* does not hold, for the
    same reason the total is not exactly monotone (below): the ratchet can
    deliver a larger slice to a downstream merchant in the shocked world and
    lift it clear of a miss it would otherwise have made. That is a rare
    second-order effect; a net shrinkage would not be.

    **Total disruption never falls materially.** This one is *not* exact, and
    pretending otherwise would be wrong. Partial settlement is gated on
    ``min_partial_fraction`` of the amount still outstanding, so the gate
    *ratchets down* each time a slice is paid. A shock elsewhere perturbs a
    merchant's cash path; if that perturbation happens to push its buffer over
    the gate once, the gate drops and it services more of an overdue debt than
    it would have unshocked. Paying down more of a late debt genuinely lowers
    the objective, so the global total can dip by a second-order amount while
    every first-order effect is worse.

    A *material* fall still indicates a real accounting defect, and this check
    has caught two: charging the lateness penalty only on full settlement (so a
    part-payer escaped the penalty on everything it did pay), and dropping the
    value-weighted delay for defaulted obligations (making a write-off cheaper
    than paying late).
    """
    graph = network.graph
    sim_config = config or SimulationConfig(horizon_hours=network.config.horizon_hours)
    baseline = LiquiditySimulator(graph, sim_config, objective).run(
        None, run_id="validate:baseline"
    )
    base_disruption = baseline.disruption or 0.0
    base_affected = set(baseline.affected_ids)
    floor = base_disruption * (1.0 - tolerance)

    targets = _probe_targets(graph, limit=3)
    causal_ok, monotone_ok, affected_ok = True, True, True
    causal_detail, monotone_detail, affected_detail = "", "", ""
    observed: dict[str, list[float]] = {}

    for merchant_id in targets:
        series = []
        for magnitude in magnitudes:
            spec = ScenarioSpec(
                family=ScenarioFamily.LIQUIDITY_DRAIN,
                magnitude=magnitude,
                target_strategy=TargetStrategy.EXPLICIT,
                explicit_targets=(merchant_id,),
            )
            scenario = build_scenario(graph, spec, dataset_id=network.dataset_version)
            result = LiquiditySimulator(
                scenario.graph, sim_config, objective
            ).run(scenario.shock, run_id="validate:shocked")
            series.append(result.disruption or 0.0)

            shocked_affected = set(result.affected_ids)
            rescued = base_affected - shocked_affected
            newly = shocked_affected - base_affected
            if len(shocked_affected) < len(base_affected) or len(newly) < len(rescued):
                affected_ok = False
                affected_detail = (
                    f"{merchant_id} at {magnitude}x: {len(shocked_affected)} affected "
                    f"vs {len(base_affected)} baseline; {len(newly)} newly damaged "
                    f"but {len(rescued)} spared {sorted(rescued)[:3]}"
                )
        observed[merchant_id] = series

        if series[0] < floor:
            causal_ok = False
            causal_detail = (
                f"{merchant_id}: shocked disruption {series[0]:.4g} is more than "
                f"{tolerance:.0%} below baseline {base_disruption:.4g}"
            )
        for smaller, larger in itertools.pairwise(series):
            if larger < smaller * (1.0 - tolerance):
                monotone_ok = False
                monotone_detail = (
                    f"{merchant_id}: disruption fell materially from {smaller:.4g} "
                    f"to {larger:.4g} as the shock grew"
                )

    return [
        Check(
            name="shock_grows_the_affected_set_on_net",
            passed=affected_ok,
            detail=affected_detail
            or f"{len(targets)} probes: damage grew on net at every magnitude",
            values={"baseline_affected": len(base_affected)},
        ),
        Check(
            name="causal_shock_does_not_reduce_disruption",
            passed=causal_ok,
            detail=causal_detail
            or f"{len(targets)} probes all within {tolerance:.0%} of baseline or above",
            values={
                "baseline": base_disruption,
                "tolerance": tolerance,
                "observed": observed,
            },
        ),
        Check(
            name="disruption_monotone_in_shock_magnitude",
            passed=monotone_ok,
            detail=monotone_detail or f"monotone across magnitudes {magnitudes}",
            values={"magnitudes": list(magnitudes), "observed": observed},
        ),
    ]


def check_intervention_conservation(
    network: SyntheticNetwork,
    *,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
) -> Check:
    """A restructure must move *when* money is owed, never how much.

    Liquidity injections legitimately add outside capital; restructuring does
    not. If total principal changed, the intervention created money, and every
    cost-effectiveness number computed from it would be fiction.
    """
    graph = network.graph
    sim_config = config or SimulationConfig(horizon_hours=network.config.horizon_hours)

    candidate = next(
        (
            o
            for o in graph.obligations
            if o.is_open and o.creditor_id != EXTERNAL_SINK and o.amount > 0
        ),
        None,
    )
    if candidate is None:
        return Check(
            name="intervention_conserves_principal",
            passed=True,
            severity="warning",
            detail="no open network obligation available to restructure",
        )

    plan = InterventionPlan(
        interventions=[
            Intervention(
                type=InterventionType.REPAYMENT_RESTRUCTURE,
                merchant_id=candidate.debtor_id,
                t=0.0,
                amount=candidate.amount,
                tranches=4,
                target_obligation_id=candidate.obligation_id,
            )
        ]
    )
    simulator = LiquiditySimulator(graph, sim_config, objective)
    simulator.run(None, plan, run_id="validate:restructure")
    children = [
        o for o in simulator.obligation_book()
        if o.parent_obligation_id == candidate.obligation_id
    ]
    total = sum(c.amount for c in children)
    ok = bool(children) and math.isclose(total, candidate.amount, rel_tol=1e-9, abs_tol=1e-6)

    return Check(
        name="intervention_conserves_principal",
        passed=ok,
        detail=(
            f"restructured INR {candidate.amount:,.2f} into {len(children)} tranches "
            f"totalling INR {total:,.2f}"
        ),
        values={"original": candidate.amount, "tranche_total": total},
    )


def check_optimum_feasible(
    network: SyntheticNetwork,
    *,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
    budget: float | None = None,
    max_actions: int = 2,
) -> Check:
    """The reported optimum must respect its own budget and cardinality limits."""
    from lce.benchmark.ground_truth import compute_ground_truth

    graph = network.graph
    sim_config = config or SimulationConfig(horizon_hours=network.config.horizon_hours)
    spec = ScenarioSpec(family=ScenarioFamily.LIQUIDITY_DRAIN, magnitude=2.5)
    scenario = build_scenario(graph, spec, dataset_id=network.dataset_version)

    truth = compute_ground_truth(
        scenario,
        true_edges=network.ground_truth_edges,
        config=sim_config,
        objective=objective,
        compute_optimum=True,
        budget=budget,
        max_actions=max_actions,
    )
    optimum = truth.optimal_intervention
    if not optimum.available:
        return Check(
            name="ground_truth_optimum_feasible",
            passed=True,
            severity="warning",
            detail=f"no exact optimum computed: {optimum.reason}",
        )

    within_budget = budget is None or optimum.cost <= budget + 1e-6
    within_cardinality = len(optimum.interventions) <= max_actions
    improves = (optimum.disruption_prevented or 0.0) >= -1e-6

    return Check(
        name="ground_truth_optimum_feasible",
        passed=within_budget and within_cardinality and improves,
        detail=(
            f"{len(optimum.interventions)} actions, cost INR {optimum.cost:,.2f}, "
            f"prevented {optimum.disruption_prevented:.4g}"
        ),
        values={
            "cost": optimum.cost,
            "n_actions": len(optimum.interventions),
            "budget": budget,
            "max_actions": max_actions,
            "disruption_prevented": optimum.disruption_prevented,
        },
    )


def _probe_targets(graph: TemporalPaymentGraph, limit: int) -> list[str]:
    ranked = sorted(
        (m for m in graph.merchant_ids if graph.out_dependencies(m)),
        key=lambda m: (-len(graph.descendants_within(m, 3)), m),
    )
    return ranked[:limit]


# -------------------------------------------------------------------- driver


def validate_dataset(
    network: SyntheticNetwork,
    *,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
    deep: bool = True,
) -> ValidationReport:
    """Run the full battery. ``deep=False`` skips the simulation-based checks."""
    diagnostics = compute_diagnostics(network)
    checks: list[Check] = [
        check_flow_consistency(network),
        check_no_negative_opening_balances(network),
        *check_structural_variation(network, diagnostics),
    ]

    if deep:
        checks.extend(check_shock_monotonicity(network, config=config, objective=objective))
        checks.append(
            check_intervention_conservation(network, config=config, objective=objective)
        )
        checks.append(check_optimum_feasible(network, config=config, objective=objective))

    report = ValidationReport(
        dataset_id=network.dataset_version, checks=checks, diagnostics=diagnostics
    )
    logger.info(
        "dataset_validated",
        dataset_id=report.dataset_id,
        passed=report.passed,
        n_failures=len(report.failures),
        n_warnings=len(report.warnings),
    )
    return report
