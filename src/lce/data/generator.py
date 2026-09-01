"""Synthetic merchant-ecosystem generator.

Why synthetic data is the right call here
-----------------------------------------
The claim this project makes is *"we can predict which merchants a shock will
reach, and pick a cheap intervention that stops it"*. To report an honest
precision/recall on that claim you need to know the true answer, and on real
payment data the latent dependency structure is unobservable. So the generator
**owns the ground truth**: it draws the pass-through coefficients
:math:`\\theta_{ij}`, the lag laws and the reliabilities itself, then emits only
the *event stream* those parameters produce. The dependency learner sees the
events and never the parameters, so scoring it is a real measurement.

Flow consistency
----------------
The single most important property of the generated network is that it is
**balanced**: a merchant's payables are funded by its receivables. Concretely,
each node is assigned a throughput :math:`T_i` (INR per horizon), keeps a margin
:math:`m_i`, and owes the rest onward:

.. math::

    P_i = T_i (1 - m_i), \\qquad
    \\sum_{j} f_{ij} = P_i, \\qquad
    T_j = \\sum_i f_{ij} \\;\\; (\\ell(j) > 0)

Layer-0 anchors draw :math:`T_i` from consumer revenue, which is exogenous;
every deeper layer's throughput is *derived* from what flows into it. Without
this, edge amounts drawn independently would leave nodes owing many times what
they ever receive, and the network would collapse with no shock applied - which
makes contagion unmeasurable, because everything is already broken.

Buffers are then sized to cover only ``coverage`` of one horizon's payables
(drawn well below 1), so a node genuinely depends on being paid. That is what
makes contagion *possible* rather than assumed.

Event generation
----------------
History is a cascading marked point process:

* Layer-0 nodes receive exogenous inflows at a Poisson rate.
* When node ``i`` receives an inflow of size ``x`` at time ``t``, then for each
  outgoing edge ``i -> j``, with probability :math:`q_{ij}` it forwards
  :math:`\\theta_{ij} x` at :math:`t + \\ell`, where :math:`\\ell` is drawn from the
  edge's lag law. That forwarded payment is itself an inflow for ``j``.
* Independently, each edge fires *baseline* recurring payments at rate
  :math:`\\mu_{ij}`, unrelated to any inflow.

The baseline stream is deliberately mixed in: a learner that simply correlates
any two adjacent payments will over-estimate :math:`\\theta`, and separating
excitation from baseline is precisely the hard part of the inference problem.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from lce.domain.edges import DependencyEdge, EdgeFeatures, LagDistribution
from lce.domain.enums import (
    MerchantSector,
    MerchantTier,
    ObligationKind,
    PaymentChannel,
    RecurrencePattern,
)
from lce.domain.events import EXTERNAL_SINK, Obligation, PaymentEvent
from lce.domain.merchant import MerchantProfile
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.logging import get_logger
from lce.seeds import SeedBundle, build_seed_bundle, config_hash

logger = get_logger(__name__)

# Bumped whenever the generative process changes in a way that alters the data
# a given config produces. Recorded in every dataset manifest so a stored
# benchmark can never be silently reinterpreted under different semantics.
GENERATOR_VERSION = "2.0.0"

HOURS_PER_DAY = 24.0
HOURS_PER_WEEK = 168.0
HOURS_PER_MONTH = 30 * HOURS_PER_DAY

# Sector adjacency: which supplier sectors a buyer sector naturally trades with.
# Drives topological clustering - without it every sector mixes uniformly and
# the graph has no community structure for a model to exploit or be fooled by.
_SECTOR_AFFINITY: dict[MerchantSector, frozenset[MerchantSector]] = {
    MerchantSector.RETAIL: frozenset(
        {MerchantSector.WHOLESALE, MerchantSector.LOGISTICS, MerchantSector.SERVICES}
    ),
    MerchantSector.SERVICES: frozenset(
        {MerchantSector.SERVICES, MerchantSector.LOGISTICS}
    ),
    MerchantSector.WHOLESALE: frozenset(
        {MerchantSector.MANUFACTURING, MerchantSector.AGRI, MerchantSector.LOGISTICS}
    ),
    MerchantSector.LOGISTICS: frozenset(
        {MerchantSector.MANUFACTURING, MerchantSector.SERVICES}
    ),
    MerchantSector.MANUFACTURING: frozenset(
        {MerchantSector.AGRI, MerchantSector.MANUFACTURING, MerchantSector.CONSTRUCTION}
    ),
    MerchantSector.CONSTRUCTION: frozenset(
        {MerchantSector.MANUFACTURING, MerchantSector.AGRI}
    ),
    MerchantSector.AGRI: frozenset({MerchantSector.AGRI, MerchantSector.LOGISTICS}),
    MerchantSector.OTHER: frozenset(),
}


def seasonal_factor(
    t: float,
    *,
    weekly_amplitude: float,
    monthly_amplitude: float,
    phase: float = 0.0,
) -> float:
    """Multiplicative demand seasonality at simulation time ``t``.

    Weekly and monthly sinusoids, floored at a small positive value so a deep
    trough scales revenue down without ever making it negative. ``phase`` is
    jittered per merchant so the whole network does not peak on the same hour -
    that would be a single global cycle, not seasonality.
    """
    weekly = weekly_amplitude * math.sin(2.0 * math.pi * t / HOURS_PER_WEEK + phase)
    monthly = monthly_amplitude * math.sin(
        2.0 * math.pi * t / HOURS_PER_MONTH + 0.5 * phase
    )
    return max(0.05, 1.0 + weekly + monthly)


def draw_amount(
    base: float,
    rng: np.random.Generator,
    *,
    log_sigma: float,
    heavy_tail_prob: float,
    heavy_tail_index: float,
    tail_cap: float = 25.0,
) -> float:
    """Payment size: log-normal body with an occasional Pareto tail draw.

    Real payment amounts are heavier-tailed than a log-normal at plausible
    sigma: most invoices sit in a band and a small minority are much larger.
    Mixing in a Pareto draw reproduces that without distorting the body, which
    matters because the tail is exactly where one missed payment goes systemic.

    The multiplier is **capped**. An uncapped Pareto at index < 2 has infinite
    variance and will eventually emit a single payment thousands of times the
    network's total throughput - which is not a heavy tail, it is a broken
    dataset that silently dominates every aggregate computed from it.
    """
    amount = base * float(rng.lognormal(0.0, log_sigma))
    if heavy_tail_prob > 0.0 and rng.random() < heavy_tail_prob:
        multiplier = float(rng.pareto(max(heavy_tail_index, 1.05)) + 1.0)
        amount *= min(multiplier, tail_cap)
    return max(1.0, amount)

# Tier -> (throughput scale INR per horizon, systemic weight, population share)
_TIER_TABLE: dict[MerchantTier, tuple[float, float, float]] = {
    MerchantTier.MICRO: (4.0e5, 0.5, 0.30),
    MerchantTier.SMALL: (2.0e6, 1.0, 0.35),
    MerchantTier.MEDIUM: (9.0e6, 2.0, 0.20),
    MerchantTier.LARGE: (4.0e7, 4.0, 0.12),
    MerchantTier.ANCHOR: (1.6e8, 8.0, 0.03),
}

_LAYER_SECTORS: list[tuple[MerchantSector, ...]] = [
    (MerchantSector.RETAIL, MerchantSector.SERVICES),
    (MerchantSector.WHOLESALE, MerchantSector.LOGISTICS),
    (MerchantSector.MANUFACTURING, MerchantSector.CONSTRUCTION),
    (MerchantSector.AGRI, MerchantSector.MANUFACTURING),
]

_RECURRENCE_PERIODS: dict[RecurrencePattern, float] = {
    RecurrencePattern.WEEKLY: HOURS_PER_WEEK,
    RecurrencePattern.BIWEEKLY: 2 * HOURS_PER_WEEK,
    RecurrencePattern.MONTHLY: 30 * HOURS_PER_DAY,
}


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    """Every knob that determines a generated dataset.

    Hashed into ``dataset_version`` so a dataset is reproducible from its id.
    """

    n_merchants: int = 60
    n_layers: int = 4
    mean_out_degree: float = 2.4
    cross_layer_edge_prob: float = 0.06
    back_edge_prob: float = 0.02
    # Caps on the *expected number* of extra edges per node. Set so they do
    # not bind on a small network - where the per-pair probability already
    # yields a sane degree - and only take effect once layers grow large
    # enough that a fixed per-pair probability would make density scale with
    # the network. That keeps SMALL, MEDIUM and LARGE topologically
    # comparable instead of progressively denser.
    max_cross_layer_degree: float = 5.0
    max_back_edge_degree: float = 1.5

    history_hours: float = 60 * HOURS_PER_DAY
    horizon_hours: float = 7 * HOURS_PER_DAY

    # Behavioural priors (ground truth for the dependency learner)
    pass_through_alpha: float = 2.2
    pass_through_beta: float = 2.6
    lag_mean_hours: float = 40.0
    lag_cv: float = 0.55
    conditional_prob_low: float = 0.40
    conditional_prob_high: float = 0.92
    baseline_rate_per_week: float = 0.45
    reliability_low: float = 0.70
    reliability_high: float = 0.99

    # Flow structure
    margin_low: float = 0.10
    margin_high: float = 0.28

    # Balance-sheet calibration: buffer as a multiple of horizon payables.
    # Drawn below 1 on purpose - nodes must be paid to stay solvent.
    coverage_low: float = 0.30
    coverage_high: float = 0.65
    credit_line_ratio: float = 0.18
    operating_floor_ratio: float = 0.06
    burn_share_of_margin: float = 0.45
    exogenous_top_up_deep: float = 0.05

    # Obligation schedule inside the simulation horizon
    obligations_per_edge: int = 2
    external_obligation_share: float = 0.20
    external_obligation_ratio: float = 0.05

    # --- economic realism -------------------------------------------------
    # Seasonality enters through the *exogenous* inflows at layer 0 and then
    # propagates down the chain by construction, so downstream inflows and
    # outflows are correlated for a structural reason rather than because a
    # correlation was imposed on them directly.
    weekly_seasonality: float = 0.25
    monthly_seasonality: float = 0.12
    seasonality_phase_jitter: float = 1.0

    # A shared multiplicative demand factor per time bucket. Without it, a
    # thousand anchors are a thousand independent draws and their average is
    # almost constant - real ecosystems have common shocks.
    common_factor_sigma: float = 0.18
    common_factor_bucket_hours: float = 24.0

    # Payment sizes. Log-normal alone has too thin a right tail for real
    # payment data, so a small share of payments is drawn from a Pareto tail.
    amount_log_sigma: float = 0.55
    heavy_tail_prob: float = 0.06
    heavy_tail_index: float = 2.2
    heavy_tail_cap: float = 25.0

    # Buyers prefer suppliers in a related sector, which is what makes sector
    # clusters appear in the topology instead of a uniformly mixed graph.
    sector_homophily: float = 2.5

    max_history_events: int = 120_000
    min_cascade_amount: float = 500.0
    seed: int = 20250101

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_merchants": self.n_merchants,
            "n_layers": self.n_layers,
            "mean_out_degree": self.mean_out_degree,
            "cross_layer_edge_prob": self.cross_layer_edge_prob,
            "back_edge_prob": self.back_edge_prob,
            "max_cross_layer_degree": self.max_cross_layer_degree,
            "max_back_edge_degree": self.max_back_edge_degree,
            "history_hours": self.history_hours,
            "horizon_hours": self.horizon_hours,
            "pass_through_alpha": self.pass_through_alpha,
            "pass_through_beta": self.pass_through_beta,
            "lag_mean_hours": self.lag_mean_hours,
            "lag_cv": self.lag_cv,
            "conditional_prob_low": self.conditional_prob_low,
            "conditional_prob_high": self.conditional_prob_high,
            "baseline_rate_per_week": self.baseline_rate_per_week,
            "reliability_low": self.reliability_low,
            "reliability_high": self.reliability_high,
            "margin_low": self.margin_low,
            "margin_high": self.margin_high,
            "coverage_low": self.coverage_low,
            "coverage_high": self.coverage_high,
            "credit_line_ratio": self.credit_line_ratio,
            "operating_floor_ratio": self.operating_floor_ratio,
            "burn_share_of_margin": self.burn_share_of_margin,
            "exogenous_top_up_deep": self.exogenous_top_up_deep,
            "obligations_per_edge": self.obligations_per_edge,
            "external_obligation_share": self.external_obligation_share,
            "external_obligation_ratio": self.external_obligation_ratio,
            "weekly_seasonality": self.weekly_seasonality,
            "monthly_seasonality": self.monthly_seasonality,
            "seasonality_phase_jitter": self.seasonality_phase_jitter,
            "common_factor_sigma": self.common_factor_sigma,
            "common_factor_bucket_hours": self.common_factor_bucket_hours,
            "amount_log_sigma": self.amount_log_sigma,
            "heavy_tail_prob": self.heavy_tail_prob,
            "heavy_tail_index": self.heavy_tail_index,
            "heavy_tail_cap": self.heavy_tail_cap,
            "sector_homophily": self.sector_homophily,
            "max_history_events": self.max_history_events,
            "min_cascade_amount": self.min_cascade_amount,
            "generator_version": GENERATOR_VERSION,
            "seed": self.seed,
        }

    @property
    def dataset_version(self) -> str:
        """Content-addressed dataset id: same config + seed => same id."""
        return f"synth-{config_hash(self.to_dict(), length=12)}"


@dataclass(slots=True)
class SyntheticNetwork:
    """A generated ecosystem plus the ground truth used to score models."""

    graph: TemporalPaymentGraph
    ground_truth_edges: dict[tuple[str, str], DependencyEdge]
    config: GeneratorConfig
    seeds: SeedBundle
    layers: dict[str, int] = field(default_factory=dict)
    throughput: dict[str, float] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def dataset_version(self) -> str:
        return self.config.dataset_version

    def true_pass_through(self) -> dict[tuple[str, str], float]:
        return {k: e.pass_through for k, e in self.ground_truth_edges.items()}

    def anchors(self) -> list[str]:
        """Layer-0 merchants - the natural origin for a demo shock."""
        return sorted(m for m, layer in self.layers.items() if layer == 0)

    def summary(self) -> dict[str, Any]:
        return {
            "dataset_version": self.dataset_version,
            "seeds": self.seeds.to_dict(),
            **self.graph.stats().to_dict(),
            **self.stats,
        }


class NetworkGenerator:
    """Builds a :class:`SyntheticNetwork` from a :class:`GeneratorConfig`."""

    def __init__(self, config: GeneratorConfig | None = None) -> None:
        self.config = config or GeneratorConfig()
        self.seeds = build_seed_bundle(self.config.seed, config_hash(self.config.to_dict()))
        # Entity ids are derived from the dataset version, never from uuid4.
        # The simulator keys its common random numbers on obligation_id, so
        # random ids would make the same config+seed produce different
        # simulation outcomes on every run - which breaks the reproducibility
        # guarantee the whole benchmark rests on.
        self._id_prefix = self.config.dataset_version.removeprefix('synth-')
        self._payment_counter = 0
        self._obligation_counter = 0

    def _next_payment_id(self) -> str:
        self._payment_counter += 1
        return f"pay_{self._id_prefix}_{self._payment_counter:08d}"

    def _next_obligation_id(self) -> str:
        self._obligation_counter += 1
        return f"obl_{self._id_prefix}_{self._obligation_counter:06d}"

    # ------------------------------------------------------------------ build

    def generate(self) -> SyntheticNetwork:
        cfg = self.config
        self._payment_counter = 0
        self._obligation_counter = 0
        graph = TemporalPaymentGraph(
            network_id=cfg.dataset_version,
            dataset_version=cfg.dataset_version,
            epoch_iso=datetime(2025, 1, 1, tzinfo=UTC).isoformat(),
        )

        layers = self._assign_layers()
        tiers, sectors = self._assign_tiers(layers)
        pairs = self._build_topology(layers, tiers, sectors)
        edges, throughput, payables, margins = self._assign_flows(layers, tiers, pairs)
        profiles = self._build_profiles(layers, tiers, sectors, throughput, payables, margins)

        graph.add_merchants(profiles.values())
        graph.add_payments(
            self._simulate_history(edges, layers, throughput), require_nodes=False
        )
        graph.add_obligations(
            self._build_obligations(edges, layers, profiles, throughput), require_nodes=False
        )
        graph.set_dependencies(edges.values())

        stats = {
            "n_layers": cfg.n_layers,
            "history_events": graph.stats().n_payment_events,
            "mean_pass_through": (
                float(np.mean([e.pass_through for e in edges.values()])) if edges else 0.0
            ),
            "mean_reliability": (
                float(np.mean([e.reliability for e in edges.values()])) if edges else 0.0
            ),
            "obligations_in_horizon": graph.stats().n_obligations,
            "total_throughput": float(sum(throughput.values())),
            "layer_sizes": {
                str(layer): sum(1 for v in layers.values() if v == layer)
                for layer in range(cfg.n_layers)
            },
        }
        logger.info("generated_network", dataset_version=cfg.dataset_version, **stats)

        return SyntheticNetwork(
            graph=graph,
            ground_truth_edges=dict(edges),
            config=cfg,
            seeds=self.seeds,
            layers=layers,
            throughput=throughput,
            stats=stats,
        )

    # -------------------------------------------------------------- topology

    def _assign_layers(self) -> dict[str, int]:
        """Assign each merchant to a supply-chain layer, widening with depth."""
        cfg = self.config
        weights = np.array([1.6**i for i in range(cfg.n_layers)], dtype=float)
        weights /= weights.sum()
        counts = np.maximum(1, np.floor(weights * cfg.n_merchants).astype(int))
        counts[-1] += cfg.n_merchants - int(counts.sum())

        layers: dict[str, int] = {}
        index = 0
        for layer, count in enumerate(counts):
            for _ in range(int(count)):
                layers[f"m{index:04d}"] = layer
                index += 1
        return layers

    def _assign_tiers(
        self, layers: dict[str, int]
    ) -> tuple[dict[str, MerchantTier], dict[str, MerchantSector]]:
        """Draw a size tier and sector per merchant, skewed by layer depth."""
        cfg = self.config
        rng = np.random.default_rng(self.seeds.behaviour_seed)
        tier_list = list(_TIER_TABLE)
        base_p = np.array([_TIER_TABLE[t][2] for t in tier_list], dtype=float)

        tiers: dict[str, MerchantTier] = {}
        sectors: dict[str, MerchantSector] = {}
        for merchant_id, layer in sorted(layers.items()):
            if layer == 0:
                tier = MerchantTier.ANCHOR if rng.random() < 0.55 else MerchantTier.LARGE
            else:
                # Deeper layers skew smaller.
                skew = base_p * np.array([1.0 + 0.5 * layer, 1.2, 1.0, 0.6, 0.25])
                skew = skew / skew.sum()
                tier = tier_list[int(rng.choice(len(tier_list), p=skew))]
            tiers[merchant_id] = tier
            options = _LAYER_SECTORS[min(layer, len(_LAYER_SECTORS) - 1)]
            sectors[merchant_id] = options[int(rng.integers(len(options)))]
        del cfg
        return tiers, sectors

    def _build_topology(
        self,
        layers: dict[str, int],
        tiers: dict[str, MerchantTier],
        sectors: dict[str, MerchantSector] | None = None,
    ) -> list[tuple[str, str]]:
        """Wire buyer -> supplier edges. Returns ordered pairs only.

        Attachment weight combines two effects:

        * **preferential attachment** on supplier size, which is what produces
          the high-in-degree bottleneck nodes a cascade concentrates through;
        * **sector homophily**, which makes those bottlenecks cluster into
          recognisable industry communities rather than spreading uniformly.
        """
        cfg = self.config
        rng = np.random.default_rng(self.seeds.topology_seed)
        sectors = sectors or {}
        by_layer: dict[int, list[str]] = {}
        for merchant_id, layer in sorted(layers.items()):
            by_layer.setdefault(layer, []).append(merchant_id)

        pairs: set[tuple[str, str]] = set()
        for merchant_id in sorted(layers):
            layer = layers[merchant_id]
            suppliers = by_layer.get(layer + 1, [])
            if suppliers:
                k = int(np.clip(rng.poisson(cfg.mean_out_degree), 1, len(suppliers)))
                weights = np.array(
                    [_TIER_TABLE[tiers[s]][0] for s in suppliers], dtype=float
                )
                if cfg.sector_homophily > 1.0 and sectors:
                    buyer_sector = sectors.get(merchant_id, MerchantSector.OTHER)
                    related = _SECTOR_AFFINITY.get(buyer_sector, frozenset())
                    affinity = np.array(
                        [
                            cfg.sector_homophily
                            if sectors.get(s, MerchantSector.OTHER) in related
                            else 1.0
                            for s in suppliers
                        ],
                        dtype=float,
                    )
                    weights = weights * affinity
                weights /= weights.sum()
                chosen = rng.choice(len(suppliers), size=k, replace=False, p=weights)
                for i in np.atleast_1d(chosen):
                    pairs.add((merchant_id, suppliers[int(i)]))

            # Cross-layer and back edges are drawn as a bounded expected *degree*,
            # not as an independent per-pair coin flip. A fixed per-pair
            # probability makes edge count grow with layer size, so density rises
            # with the network: at 100 merchants it adds ~2 edges per node, at
            # 1,000 it adds ~24, and at 10,000 it is unusable. That also makes the
            # benchmark scales incomparable, since SMALL and LARGE would differ in
            # topology as well as size.
            deeper_pool = [
                candidate
                for deeper in range(layer + 2, cfg.n_layers)
                for candidate in by_layer.get(deeper, [])
            ]
            pairs.update(
                (merchant_id, candidate)
                for candidate in _sample_extra(
                    deeper_pool, cfg.cross_layer_edge_prob, cfg.max_cross_layer_degree, rng
                )
            )

            shallower_pool = [
                candidate
                for shallower in range(layer)
                for candidate in by_layer.get(shallower, [])
            ]
            pairs.update(
                (merchant_id, candidate)
                for candidate in _sample_extra(
                    shallower_pool, cfg.back_edge_prob, cfg.max_back_edge_degree, rng
                )
            )

        return sorted(p for p in pairs if p[0] != p[1])

    # ------------------------------------------------------------ flow model

    def _assign_flows(
        self,
        layers: dict[str, int],
        tiers: dict[str, MerchantTier],
        pairs: list[tuple[str, str]],
    ) -> tuple[
        dict[tuple[str, str], DependencyEdge],
        dict[str, float],
        dict[str, float],
        dict[str, float],
    ]:
        """Propagate throughput down the layers and size every edge from it.

        Processing strictly in layer order means a node's payable budget is
        fixed before it can be increased by a back-edge, which keeps the
        allocation conservative (a back-edge only ever adds unbudgeted inflow).
        """
        cfg = self.config
        rng = np.random.default_rng(self.seeds.behaviour_seed + 3)

        out_pairs: dict[str, list[str]] = {}
        for source, target in pairs:
            out_pairs.setdefault(source, []).append(target)

        throughput: dict[str, float] = {}
        for merchant_id, layer in layers.items():
            if layer == 0:
                scale, _, _ = _TIER_TABLE[tiers[merchant_id]]
                throughput[merchant_id] = float(scale * rng.lognormal(0.0, 0.30))
            else:
                throughput[merchant_id] = 0.0

        margins: dict[str, float] = {
            m: float(rng.uniform(cfg.margin_low, cfg.margin_high)) for m in sorted(layers)
        }
        payables: dict[str, float] = dict.fromkeys(layers, 0.0)
        edge_flow: dict[tuple[str, str], float] = {}

        for layer in range(cfg.n_layers):
            for merchant_id in sorted(m for m, v in layers.items() if v == layer):
                targets = sorted(out_pairs.get(merchant_id, []))
                if not targets:
                    continue
                budget = throughput[merchant_id] * (1.0 - margins[merchant_id])
                if budget <= 0:
                    continue
                # Dirichlet split gives an uneven but non-degenerate allocation.
                shares = rng.dirichlet(np.full(len(targets), 2.5))
                for target, share in zip(targets, shares, strict=True):
                    flow = float(budget * share)
                    edge_flow[(merchant_id, target)] = flow
                    payables[merchant_id] += flow
                    throughput[target] += flow

        edges = {
            pair: self._draw_edge(pair, flow, rng)
            for pair, flow in sorted(edge_flow.items())
        }
        return edges, throughput, payables, margins

    def _draw_edge(
        self, pair: tuple[str, str], horizon_flow: float, rng: np.random.Generator
    ) -> DependencyEdge:
        """Draw the ground-truth behavioural law of one link."""
        cfg = self.config
        source, target = pair
        pass_through = float(
            np.clip(rng.beta(cfg.pass_through_alpha, cfg.pass_through_beta), 0.02, 0.85)
        )
        lag_mean = float(cfg.lag_mean_hours * rng.lognormal(0.0, 0.28))
        lag = LagDistribution.from_mean_cv(lag_mean, cv=cfg.lag_cv, floor_hours=2.0)
        recurrence = [
            RecurrencePattern.WEEKLY,
            RecurrencePattern.BIWEEKLY,
            RecurrencePattern.MONTHLY,
        ][int(rng.integers(3))]
        per_obligation = horizon_flow / max(cfg.obligations_per_edge, 1)

        return DependencyEdge(
            source_id=source,
            target_id=target,
            features=EdgeFeatures(
                mean_amount=max(1.0, per_obligation),
                std_amount=max(1.0, per_obligation) * float(rng.uniform(0.12, 0.40)),
                recurrence=recurrence,
                period_hours=_RECURRENCE_PERIODS[recurrence],
                regularity=float(rng.uniform(0.35, 0.9)),
            ),
            lag=lag,
            reliability=float(rng.uniform(cfg.reliability_low, cfg.reliability_high)),
            pass_through=pass_through,
            conditional_probability=float(
                rng.uniform(cfg.conditional_prob_low, cfg.conditional_prob_high)
            ),
            excitation_alpha=pass_through,
            excitation_decay=1.0 / max(lag.mean_hours, 1.0),
            base_intensity=float(
                cfg.baseline_rate_per_week / HOURS_PER_WEEK * rng.uniform(0.5, 1.5)
            ),
            is_ground_truth=True,
            estimator="generator",
            confidence=1.0,
            metadata={"horizon_flow": horizon_flow},
        )

    # ------------------------------------------------------------- profiles

    def _build_profiles(
        self,
        layers: dict[str, int],
        tiers: dict[str, MerchantTier],
        sectors: dict[str, MerchantSector],
        throughput: dict[str, float],
        payables: dict[str, float],
        margins: dict[str, float],
    ) -> dict[str, MerchantProfile]:
        """Size each balance sheet against the payable load the node carries."""
        cfg = self.config
        rng = np.random.default_rng(self.seeds.behaviour_seed + 7)
        profiles: dict[str, MerchantProfile] = {}

        for merchant_id in sorted(layers):
            layer = layers[merchant_id]
            tier = tiers[merchant_id]
            _, weight, _ = _TIER_TABLE[tier]
            load = payables.get(merchant_id, 0.0)
            flow = throughput.get(merchant_id, 0.0)
            coverage = float(rng.uniform(cfg.coverage_low, cfg.coverage_high))

            # Buffer b = L + K - floor should equal coverage x horizon payables.
            buffer_target = max(load * coverage, flow * 0.05, 2.5e4)
            opening = buffer_target / (
                1.0 + cfg.credit_line_ratio - cfg.operating_floor_ratio
            )

            # Layer-0 throughput is consumer revenue and therefore exogenous.
            # Deeper layers earn almost everything through the network.
            if layer == 0:
                inflow_rate = flow / cfg.horizon_hours
            else:
                inflow_rate = flow * cfg.exogenous_top_up_deep / cfg.horizon_hours

            # Operating burn consumes part of the retained margin, so a healthy
            # node is roughly cash-neutral over the horizon.
            burn_rate = flow * margins[merchant_id] * cfg.burn_share_of_margin / (
                cfg.horizon_hours
            )

            profiles[merchant_id] = MerchantProfile(
                merchant_id=merchant_id,
                name=f"Merchant {merchant_id[1:]}",
                sector=sectors[merchant_id],
                tier=tier,
                opening_balance=float(opening),
                operating_floor=float(opening * cfg.operating_floor_ratio),
                credit_limit=float(opening * cfg.credit_line_ratio),
                exogenous_inflow_rate=float(max(0.0, inflow_rate)),
                operating_burn_rate=float(max(0.0, burn_rate)),
                payment_discipline=float(np.clip(rng.beta(9.0, 1.2), 0.60, 0.995)),
                stress_threshold_ratio=float(rng.uniform(0.15, 0.35)),
                systemic_weight=weight,
                metadata={
                    "layer": layer,
                    "throughput": flow,
                    "payable_load": load,
                    "coverage_target": coverage,
                    "margin": margins[merchant_id],
                },
            )
        return profiles

    # --------------------------------------------------------- event history

    def _simulate_history(
        self,
        edges: dict[tuple[str, str], DependencyEdge],
        layers: dict[str, int],
        throughput: dict[str, float],
    ) -> list[PaymentEvent]:
        """Run the cascading point process over the history window."""
        cfg = self.config
        rng = np.random.default_rng(self.seeds.event_seed)
        t_start = -cfg.history_hours

        out_edges: dict[str, list[DependencyEdge]] = {}
        for (source, _), edge in sorted(edges.items()):
            out_edges.setdefault(source, []).append(edge)

        payments: list[PaymentEvent] = []
        frontier: list[tuple[float, str, float]] = []

        # A shared demand factor per time bucket. Every anchor's revenue is
        # scaled by the same draw within a bucket, so inflows across the network
        # are genuinely correlated rather than independent noise that averages
        # away at scale.
        n_buckets = int(math.ceil(cfg.history_hours / cfg.common_factor_bucket_hours)) + 1
        common_factor = (
            rng.lognormal(0.0, cfg.common_factor_sigma, size=n_buckets)
            if cfg.common_factor_sigma > 0
            else np.ones(n_buckets)
        )

        def _common(t: float) -> float:
            index = int((t - t_start) / cfg.common_factor_bucket_hours)
            return float(common_factor[min(max(index, 0), n_buckets - 1)])

        # Seed the cascade with exogenous revenue at the anchors.
        for merchant_id, layer in sorted(layers.items()):
            if layer != 0:
                continue
            n_per_horizon = 8
            rate = n_per_horizon / cfg.horizon_hours
            n = max(1, int(rng.poisson(rate * cfg.history_hours)))
            base = throughput.get(merchant_id, 0.0) / n_per_horizon
            if base <= 0:
                continue
            phase = float(rng.uniform(0.0, cfg.seasonality_phase_jitter * 2.0 * math.pi))
            for t in np.sort(rng.uniform(t_start, 0.0, size=n)):
                t = float(t)
                scale = base * seasonal_factor(
                    t,
                    weekly_amplitude=cfg.weekly_seasonality,
                    monthly_amplitude=cfg.monthly_seasonality,
                    phase=phase,
                ) * _common(t)
                amount = draw_amount(
                    scale,
                    rng,
                    log_sigma=cfg.amount_log_sigma,
                    heavy_tail_prob=cfg.heavy_tail_prob,
                    heavy_tail_index=cfg.heavy_tail_index,
                    tail_cap=cfg.heavy_tail_cap,
                )
                frontier.append((t, merchant_id, amount))
                # Consumer revenue is *observable* - a payment platform sees the
                # money arriving even though its payer is outside the modelled
                # merchant set. Emitting it matters: without an inflow stream
                # for the anchors, no learner can attribute their outflows to
                # anything, and pass-through is unidentifiable at layer 0.
                payments.append(
                    PaymentEvent(
                        event_id=self._next_payment_id(),
                        payer_id=EXTERNAL_SINK,
                        payee_id=merchant_id,
                        amount=amount,
                        t=float(t),
                        channel=_channel_for(amount, rng),
                        metadata={"driver": "exogenous"},
                    )
                )

        # Cascade forward: each inflow may excite outgoing payments, which are
        # themselves inflows one layer down. The frontier is a simple queue -
        # ordering does not matter because each item is handled independently.
        cursor = 0
        while cursor < len(frontier) and len(payments) < cfg.max_history_events:
            t, merchant_id, amount = frontier[cursor]
            cursor += 1
            for edge in out_edges.get(merchant_id, []):
                if rng.random() >= edge.conditional_probability:
                    continue
                lag = float(np.asarray(edge.lag.sample(rng)))
                t_out = t + lag
                if t_out >= 0.0:
                    continue
                out_amount = float(
                    max(1.0, edge.pass_through * amount * rng.lognormal(0.0, 0.22))
                )
                payments.append(
                    PaymentEvent(
                        event_id=self._next_payment_id(),
                        payer_id=merchant_id,
                        payee_id=edge.target_id,
                        amount=out_amount,
                        t=t_out,
                        channel=_channel_for(out_amount, rng),
                        metadata={"driver": "excitation"},
                    )
                )
                if out_amount >= cfg.min_cascade_amount:
                    frontier.append((t_out, edge.target_id, out_amount))

        # Baseline recurring stream, independent of any inflow. This is the
        # confound the dependency learner has to see through.
        for (source, target), edge in sorted(edges.items()):
            n = int(rng.poisson(max(edge.base_intensity * cfg.history_hours, 0.0)))
            if n <= 0:
                continue
            period = edge.features.period_hours or HOURS_PER_WEEK
            phase = float(rng.uniform(0.0, period))
            for k in range(n):
                t = t_start + phase + k * period * float(rng.uniform(0.85, 1.15))
                if t >= 0.0:
                    break
                amount = float(max(1.0, edge.features.mean_amount * rng.lognormal(0.0, 0.25)))
                payments.append(
                    PaymentEvent(
                        event_id=self._next_payment_id(),
                        payer_id=source,
                        payee_id=target,
                        amount=amount,
                        t=float(t),
                        channel=_channel_for(amount, rng),
                        metadata={"driver": "baseline"},
                    )
                )

        payments.sort(key=lambda e: (e.t, e.event_id))
        return payments

    # ---------------------------------------------------------- obligations

    def _build_obligations(
        self,
        edges: dict[tuple[str, str], DependencyEdge],
        layers: dict[str, int],
        profiles: dict[str, MerchantProfile],
        throughput: dict[str, float],
    ) -> list[Obligation]:
        """Schedule the commitments falling due inside the simulation horizon.

        Deadlines are staggered by layer: a merchant's receivables are due
        before its own payables, which is the ordering a working supply chain
        actually has. Without the stagger every node would be asked to pay
        before anyone paid it, and the baseline would fail for reasons that have
        nothing to do with contagion.
        """
        cfg = self.config
        rng = np.random.default_rng(self.seeds.event_seed + 11)
        obligations: list[Obligation] = []

        step = cfg.horizon_hours / (cfg.n_layers + 1)
        window = step * 1.4

        for (source, target), edge in sorted(edges.items()):
            layer = layers[source]
            start = layer * step
            flow = float(edge.metadata.get("horizon_flow", 0.0))
            if flow <= 0:
                continue
            for k in range(cfg.obligations_per_edge):
                span = window / max(cfg.obligations_per_edge, 1)
                due = float(
                    np.clip(
                        rng.uniform(start + k * span, start + (k + 1) * span),
                        0.5,
                        cfg.horizon_hours - 1.0,
                    )
                )
                amount = float(
                    max(
                        1.0,
                        (flow / cfg.obligations_per_edge) * rng.lognormal(0.0, 0.18),
                    )
                )
                obligations.append(
                    Obligation(
                        obligation_id=self._next_obligation_id(),
                        debtor_id=source,
                        creditor_id=target,
                        amount=amount,
                        issued_t=-float(rng.uniform(24.0, 30 * HOURS_PER_DAY)),
                        due_t=due,
                        kind=ObligationKind.TRADE_PAYABLE,
                        priority=0,
                        metadata={"edge": f"{source}->{target}"},
                    )
                )

        # Non-network sinks: payroll and tax. They consume the same buffer as
        # trade payables and outrank them, but a miss on them starves no
        # modelled merchant - so they must not manufacture fake contagion.
        for merchant_id in sorted(profiles):
            if rng.random() >= cfg.external_obligation_share:
                continue
            amount = float(
                throughput.get(merchant_id, 0.0) * cfg.external_obligation_ratio
            )
            if amount <= 1.0:
                continue
            obligations.append(
                Obligation(
                    obligation_id=self._next_obligation_id(),
                    debtor_id=merchant_id,
                    creditor_id=EXTERNAL_SINK,
                    amount=amount,
                    issued_t=-float(rng.uniform(24.0, 240.0)),
                    due_t=float(rng.uniform(0.0, cfg.horizon_hours)),
                    kind=ObligationKind.PAYROLL,
                    priority=1,  # payroll outranks trade payables
                    metadata={"external": True},
                )
            )

        obligations.sort(key=lambda o: (o.due_t, o.obligation_id))
        return obligations


def _sample_extra(
    pool: list[str],
    probability: float,
    max_degree: float,
    rng: np.random.Generator,
) -> list[str]:
    """Draw extra edges at a bounded expected degree.

    The effective per-pair probability is capped so the expected number of extra
    edges per node never exceeds ``max_degree``, however large the candidate
    pool grows.
    """
    if not pool or probability <= 0 or max_degree <= 0:
        return []
    effective = min(probability, max_degree / len(pool))
    expected = effective * len(pool)
    count = min(len(pool), int(rng.poisson(expected)))
    if count <= 0:
        return []
    chosen = rng.choice(len(pool), size=count, replace=False)
    return [pool[int(i)] for i in np.atleast_1d(chosen)]


def _channel_for(amount: float, rng: np.random.Generator) -> PaymentChannel:
    """Pick a plausible rail for an amount (UPI is capped in practice)."""
    if amount < 1.0e5:
        return PaymentChannel.UPI if rng.random() < 0.7 else PaymentChannel.IMPS
    if amount < 2.0e6:
        return PaymentChannel.NEFT if rng.random() < 0.6 else PaymentChannel.IMPS
    return PaymentChannel.RTGS


def generate_network(config: GeneratorConfig | None = None) -> SyntheticNetwork:
    """Convenience wrapper: build a synthetic ecosystem from a config."""
    return NetworkGenerator(config).generate()
