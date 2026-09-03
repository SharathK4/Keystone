"""Systemic importance ranking.

The closing question of the demo: *which merchants are structurally load-bearing,
independent of their own size?*

Two views are combined, and they answer different questions:

**Simulated** - :math:`\\mathrm{SI}_i = D(G, S_i)` where :math:`S_i` is a
*standardised* shock at ``i`` (a fixed fraction of that node's own buffer). Using
a relative rather than absolute shock is the whole point: shocking every node
with the same rupee amount would just rediscover which merchants are large.

**Structural** - Katz centrality on the pass-through-weighted dependency graph.
Cheap, shock-free, and computable for a node that has never been shocked.

The two disagree in an informative way. A node can be structurally central yet
systemically unimportant because its downstream neighbours are all well
capitalised; the simulated measure catches that and the structural one cannot.

Not a size proxy - and that is measured
---------------------------------------
The obvious objection to any systemic ranking is that it has rediscovered
"large". Three cheap baselines are therefore computed alongside it - transaction
degree, payment throughput (the GMV proxy), and horizon cash deficit - and the
rank correlation between each and the simulated measure is reported. If the
correlation with throughput were near one, the ranking would be a size ranking
wearing a different name, and the number says so rather than leaving it to
assertion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lce.config import ObjectiveSettings
from lce.domain.events import EXTERNAL_SINK
from lce.domain.objectives import systemic_importance
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.simulation.engine import LiquiditySimulator, SimulationConfig
from lce.simulation.scenarios import unit_shock

logger = get_logger(__name__)


@dataclass(slots=True)
class SystemicProbe:
    """What a standardised shock at one merchant actually did to the network."""

    merchant_id: str
    marginal_disruption: float
    downstream_affected: int
    downstream_delayed_value: float
    cascade_depth: int
    time_to_impact_hours: float | None
    throughput: float
    scale: float = 1.0
    """The merchant's own economic size: observed throughput, floored at its
    liquidity buffer. The floor matters - a merchant with no observed payments
    would otherwise divide by ~zero and top the normalised ranking on the
    strength of having no history."""

    @property
    def scale_normalised(self) -> float:
        """``SI_i`` per rupee of the merchant's own scale.

        The measure that separates *load-bearing* from *large*: a merchant twice
        the size causing twice the damage is unremarkable, and dividing it out is
        what makes the remainder interesting.
        """
        return self.marginal_disruption / max(self.scale, 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "marginal_disruption": self.marginal_disruption,
            "scale_normalised": self.scale_normalised,
            "scale": self.scale,
            "downstream_affected": self.downstream_affected,
            "downstream_delayed_value": self.downstream_delayed_value,
            "cascade_depth": self.cascade_depth,
            "time_to_impact_hours": self.time_to_impact_hours,
            "throughput": self.throughput,
        }


@dataclass(slots=True)
class SystemicRanking:
    """Per-merchant systemic importance, simulated and structural."""

    simulated: dict[str, float]
    normalised: dict[str, float]
    structural: dict[str, float]
    downstream_counts: dict[str, int]
    baseline_disruption: float
    shock_fraction: float
    probes: dict[str, SystemicProbe] = field(default_factory=dict)
    baselines: dict[str, dict[str, float]] = field(default_factory=dict)

    def ranked(self, limit: int | None = None) -> list[tuple[str, float]]:
        order = sorted(self.normalised.items(), key=lambda kv: (-kv[1], kv[0]))
        return order[:limit] if limit else order

    def ranked_by_scale(self, limit: int | None = None) -> list[tuple[str, float]]:
        """Ranked by damage per unit of own throughput."""
        order = sorted(
            ((m, p.scale_normalised) for m, p in self.probes.items()),
            key=lambda kv: (-kv[1], kv[0]),
        )
        return order[:limit] if limit else order

    def baseline_correlations(self) -> dict[str, float]:
        """Spearman correlation between ``SI`` and each cheap baseline.

        High correlation with ``throughput`` would mean the ranking is a size
        ranking; high correlation with ``degree`` would mean it is a centrality
        ranking. Either is a legitimate finding, and reporting it is the only way
        the claim that ``SI`` adds something can be checked.
        """
        if not self.probes:
            return {}
        merchants = sorted(self.probes)
        target = np.array([self.simulated.get(m, 0.0) for m in merchants], dtype=float)
        out: dict[str, float] = {}
        for name, values in sorted(self.baselines.items()):
            other = np.array([values.get(m, 0.0) for m in merchants], dtype=float)
            out[name] = _spearman(target, other)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_disruption": self.baseline_disruption,
            "shock_fraction": self.shock_fraction,
            "baseline_rank_correlation": self.baseline_correlations(),
            "ranking": [
                {
                    "merchant_id": merchant_id,
                    "systemic_importance": score,
                    "marginal_disruption": self.simulated.get(merchant_id, 0.0),
                    "structural_centrality": self.structural.get(merchant_id, 0.0),
                    "downstream_nodes": self.downstream_counts.get(merchant_id, 0),
                    **(
                        self.probes[merchant_id].to_dict()
                        if merchant_id in self.probes
                        else {}
                    ),
                }
                for merchant_id, score in self.ranked()
            ],
        }


def compute_systemic_importance(
    graph: TemporalPaymentGraph,
    *,
    config: SimulationConfig | None = None,
    objective: ObjectiveSettings | None = None,
    shock_fraction: float = 1.5,
    max_hops: int = 4,
    merchants: list[str] | None = None,
) -> SystemicRanking:
    """Sweep a standardised shock across merchants and rank the damage.

    Cost is one simulation per merchant plus one baseline, so this is the most
    expensive routine in the system. Pass ``merchants`` to restrict the sweep.
    """
    sim_config = config or SimulationConfig()
    targets = sorted(merchants) if merchants is not None else sorted(graph.merchant_ids)

    baseline = LiquiditySimulator(graph, sim_config, objective).run(None, run_id="si_base")
    baseline_disruption = baseline.disruption or 0.0

    marginal: dict[str, float] = {}
    probes: dict[str, SystemicProbe] = {}
    throughput = payment_throughput(graph)

    for merchant_id in targets:
        shock = unit_shock(graph, merchant_id, fraction_of_buffer=shock_fraction)
        result = LiquiditySimulator(graph, sim_config, objective).run(
            shock, run_id="si_probe"
        )
        # Marginal over the undisturbed baseline: the damage this shock *caused*,
        # not the network's standing level of distress.
        damage = max(0.0, (result.disruption or 0.0) - baseline_disruption)
        marginal[merchant_id] = damage

        # Everything below comes from the same run, so the richer measure costs
        # no extra simulations - only the scalar was being thrown away before.
        attributable = set(result.affected_ids) - set(baseline.affected_ids)
        downstream = attributable - {merchant_id}
        hits = {
            m: t for m, t in result.hit_times().items() if m in downstream
        }
        depths = [
            outcome.hop_distance
            for m, outcome in result.outcomes.items()
            if m in downstream and outcome.hop_distance is not None
        ]
        probes[merchant_id] = SystemicProbe(
            merchant_id=merchant_id,
            marginal_disruption=damage,
            downstream_affected=len(downstream),
            downstream_delayed_value=sum(
                max(
                    0.0,
                    outcome.value_delayed
                    - baseline.outcomes[m].value_delayed,
                )
                for m, outcome in result.outcomes.items()
                if m in downstream and m in baseline.outcomes
            ),
            cascade_depth=max(depths) if depths else 0,
            time_to_impact_hours=min(hits.values()) if hits else None,
            throughput=throughput.get(merchant_id, 0.0),
            scale=max(
                throughput.get(merchant_id, 0.0),
                graph.merchant(merchant_id).initial_buffer,
                1.0,
            ),
        )

    structural = graph.structural_centrality()
    downstream_counts = {
        merchant_id: len(graph.descendants_within(merchant_id, max_hops))
        for merchant_id in targets
    }

    logger.info(
        "systemic_importance_computed",
        n_merchants=len(targets),
        baseline_disruption=baseline_disruption,
    )
    return SystemicRanking(
        simulated=marginal,
        normalised=systemic_importance(marginal),
        structural={k: structural.get(k, 0.0) for k in targets},
        downstream_counts=downstream_counts,
        baseline_disruption=baseline_disruption,
        shock_fraction=shock_fraction,
        probes=probes,
        baselines=cheap_baselines(graph, targets, horizon=sim_config.horizon_hours),
    )


# ------------------------------------------------------------------ baselines


def payment_throughput(graph: TemporalPaymentGraph) -> dict[str, float]:
    """Total observed payment value flowing through each merchant.

    The GMV proxy. Used only as a *denominator* and as a baseline to correlate
    against - never as the ranking itself, which is the whole point of the
    exercise.
    """
    totals: dict[str, float] = dict.fromkeys(graph.merchant_ids, 0.0)
    for event in graph.payment_events:
        if event.payer_id in totals:
            totals[event.payer_id] += event.amount
        if event.payee_id in totals:
            totals[event.payee_id] += event.amount
    return totals


def cheap_baselines(
    graph: TemporalPaymentGraph, merchants: list[str], *, horizon: float
) -> dict[str, dict[str, float]]:
    """The three rankings ``SI`` must be shown not to be: size, degree, deficit."""
    throughput = payment_throughput(graph)

    degree: dict[str, float] = dict.fromkeys(merchants, 0.0)
    for payer, payee in graph.distinct_pairs():
        if EXTERNAL_SINK in (payer, payee):
            continue
        if payer in degree:
            degree[payer] += 1.0
        if payee in degree:
            degree[payee] += 1.0

    deficit: dict[str, float] = {}
    for merchant_id in merchants:
        payables = sum(
            o.outstanding
            for o in graph.payables_of(merchant_id)
            if o.is_open and o.due_t <= horizon
        )
        deficit[merchant_id] = payables - graph.merchant(merchant_id).initial_buffer

    return {
        "throughput": {m: throughput.get(m, 0.0) for m in merchants},
        "degree": degree,
        "cash_deficit": deficit,
    }


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Rank correlation; 0.0 rather than NaN when either side is constant."""
    if a.size < 2:
        return 0.0
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    if float(np.std(ra)) < 1e-12 or float(np.std(rb)) < 1e-12:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])
