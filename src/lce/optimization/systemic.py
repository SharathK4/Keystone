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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lce.config import ObjectiveSettings
from lce.domain.objectives import systemic_importance
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.simulation.engine import LiquiditySimulator, SimulationConfig
from lce.simulation.scenarios import unit_shock

logger = get_logger(__name__)


@dataclass(slots=True)
class SystemicRanking:
    """Per-merchant systemic importance, simulated and structural."""

    simulated: dict[str, float]
    normalised: dict[str, float]
    structural: dict[str, float]
    downstream_counts: dict[str, int]
    baseline_disruption: float
    shock_fraction: float

    def ranked(self, limit: int | None = None) -> list[tuple[str, float]]:
        order = sorted(self.normalised.items(), key=lambda kv: (-kv[1], kv[0]))
        return order[:limit] if limit else order

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_disruption": self.baseline_disruption,
            "shock_fraction": self.shock_fraction,
            "ranking": [
                {
                    "merchant_id": merchant_id,
                    "systemic_importance": score,
                    "marginal_disruption": self.simulated.get(merchant_id, 0.0),
                    "structural_centrality": self.structural.get(merchant_id, 0.0),
                    "downstream_nodes": self.downstream_counts.get(merchant_id, 0),
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
    for merchant_id in targets:
        shock = unit_shock(graph, merchant_id, fraction_of_buffer=shock_fraction)
        result = LiquiditySimulator(graph, sim_config, objective).run(
            shock, run_id="si_probe"
        )
        # Marginal over the undisturbed baseline: the damage this shock *caused*,
        # not the network's standing level of distress.
        marginal[merchant_id] = max(0.0, (result.disruption or 0.0) - baseline_disruption)

    structural = graph.structural_centrality()
    downstream = {
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
        downstream_counts=downstream,
        baseline_disruption=baseline_disruption,
        shock_fraction=shock_fraction,
    )
