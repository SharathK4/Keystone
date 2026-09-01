"""Analytic contagion predictors.

These answer the demo's second question - *given a shock, who is exposed, and
when does it reach them?* - without running the simulator. That matters for two
reasons: the optimiser needs a cheap ranking to build its candidate set, and a
predictor that is independent of the simulator is the only way to make the
evaluation meaningful. Scoring the simulator against itself would prove nothing.

Two models are provided.

``LinearThresholdPropagator``
    A magnitude-carrying, DebtRank-style relaxation. A node absorbs incoming
    shortfall up to its liquidity buffer and passes on only the **excess**:

    .. math::

        d_j = \\sum_{i \\in \\mathcal{N}^-(j)} \\min\\!\\big(w_{ij}\\, e_i,\\; A_{ij}\\big),
        \\qquad e_j = (d_j - b_j)^+

    where :math:`w_{ij} = \\theta_{ij} / \\sum_k \\theta_{ik}` is the normalised
    pass-through share, :math:`A_{ij}` is what ``i`` actually owes ``j`` inside
    the horizon (a shortfall cannot exceed the obligation it rides on), and
    :math:`b_j` is the buffer. Capping at the obligation is what stops the naive
    version from propagating fictitious money through links with no live
    commitment.

    The exposure score is :math:`\\sigma(k(d_j/b_j - 1))`, so a score of 0.5
    corresponds *exactly* to "incoming shortfall equals the buffer" - the real
    constraint condition - rather than to an arbitrary cut-off.

``HawkesCascadePredictor``
    A probabilistic reachability model using only the excitation parameters:
    the chance a node is reached at all, ignoring magnitudes, via
    :math:`1 - \\prod_i (1 - p_i q_{ij} F_{ij}(\\cdot))`. Cheaper and
    magnitude-blind - useful precisely because it fails differently from the
    threshold model, which makes their comparison informative.

Timing in both models comes from the edge lag laws, relaxed shortest-path style
over expected lag, and floored at the earliest live obligation deadline on the
link: a shock cannot bite before there is something to miss.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from lce.domain.enums import PredictorKind
from lce.domain.events import EXTERNAL_SINK
from lce.domain.prediction import ModelPrediction, NodeExposure
from lce.domain.shock import Shock
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.seeds import config_hash

# Steepness of the logistic mapping shortfall/buffer -> exposure score.
DEFAULT_LOGISTIC_K = 3.0


@dataclass(frozen=True, slots=True)
class PropagationConfig:
    """Predictor hyper-parameters."""

    max_hops: int = 6
    horizon_hours: float = 168.0
    logistic_k: float = DEFAULT_LOGISTIC_K
    threshold: float = 0.5
    min_transmit: float = 1.0
    cap_by_obligation: bool = True
    use_reliability: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_hops": self.max_hops,
            "horizon_hours": self.horizon_hours,
            "logistic_k": self.logistic_k,
            "threshold": self.threshold,
            "min_transmit": self.min_transmit,
            "cap_by_obligation": self.cap_by_obligation,
            "use_reliability": self.use_reliability,
        }


def _logistic(x: float, k: float) -> float:
    return 1.0 / (1.0 + math.exp(-k * max(-50.0, min(50.0, x))))


def _buffer_of(graph: TemporalPaymentGraph, merchant_id: str) -> float:
    profile = graph.merchant(merchant_id)
    return max(profile.initial_buffer, 1.0)


def _obligation_amounts(
    graph: TemporalPaymentGraph, horizon: float
) -> dict[tuple[str, str], float]:
    """Live commitment value per directed pair inside the horizon."""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for obligation in graph.obligations:
        if not obligation.is_open or obligation.due_t > horizon:
            continue
        if EXTERNAL_SINK in (obligation.debtor_id, obligation.creditor_id):
            continue
        totals[(obligation.debtor_id, obligation.creditor_id)] += obligation.outstanding
    return dict(totals)


def _earliest_due(
    graph: TemporalPaymentGraph, horizon: float
) -> dict[tuple[str, str], float]:
    earliest: dict[tuple[str, str], float] = {}
    for obligation in graph.obligations:
        if not obligation.is_open or obligation.due_t > horizon:
            continue
        key = (obligation.debtor_id, obligation.creditor_id)
        if key not in earliest or obligation.due_t < earliest[key]:
            earliest[key] = obligation.due_t
    return earliest


class LinearThresholdPropagator:
    """Magnitude-carrying shortfall propagation with buffer absorption."""

    kind = PredictorKind.LINEAR_THRESHOLD

    def __init__(self, config: PropagationConfig | None = None) -> None:
        self.config = config or PropagationConfig()

    def predict(
        self,
        graph: TemporalPaymentGraph,
        shock: Shock,
        *,
        model_version: str = "v1",
        run_id: str | None = None,
    ) -> ModelPrediction:
        cfg = self.config
        horizon = cfg.horizon_hours

        obligations = _obligation_amounts(graph, horizon) if cfg.cap_by_obligation else {}
        earliest_due = _earliest_due(graph, horizon)

        # Normalised outgoing pass-through shares per node.
        shares: dict[str, dict[str, float]] = {}
        for merchant_id in graph.merchant_ids:
            out = graph.out_dependencies(merchant_id)
            total = sum(e.pass_through for e in out)
            if total <= 0:
                shares[merchant_id] = {}
                continue
            shares[merchant_id] = {e.target_id: e.pass_through / total for e in out}

        # Seed: the directly shocked nodes.
        incoming: dict[str, float] = defaultdict(float)
        # Excess already pushed downstream per node, so that a node reached
        # again on a later hop only propagates the *additional* unabsorbed
        # amount. Without this the same rupee would be forwarded once per hop.
        propagated: dict[str, float] = defaultdict(float)
        hit_time: dict[str, float] = {}
        sources: dict[str, list[tuple[str, float]]] = defaultdict(list)
        hop_of: dict[str, int] = {}

        frontier: set[str] = set()
        for component in shock.components:
            incoming[component.merchant_id] += component.magnitude
            hit_time[component.merchant_id] = component.t
            hop_of[component.merchant_id] = 0
            frontier.add(component.merchant_id)

        # Relax outward, hop by hop, propagating only unabsorbed excess.
        for hop in range(1, cfg.max_hops + 1):
            next_frontier: set[str] = set()
            for node in sorted(frontier):
                buffer = _buffer_of(graph, node)
                excess = max(0.0, incoming[node] - buffer) - propagated[node]
                if excess <= cfg.min_transmit:
                    continue
                propagated[node] += excess

                for target, share in shares.get(node, {}).items():
                    transmitted = excess * share
                    if cfg.cap_by_obligation:
                        transmitted = min(
                            transmitted, obligations.get((node, target), 0.0)
                        )
                    if transmitted <= cfg.min_transmit:
                        continue

                    edge = graph.dependency(node, target)
                    if edge is None:
                        continue
                    if cfg.use_reliability:
                        # A less reliable payer transmits stress more readily:
                        # it has less slack to absorb the miss itself.
                        transmitted *= 1.0 + (1.0 - edge.reliability)

                    arrival = hit_time.get(node, 0.0) + edge.lag.mean_hours
                    due = earliest_due.get((node, target))
                    if due is not None:
                        arrival = max(arrival, due)
                    if arrival > horizon:
                        continue

                    incoming[target] += transmitted
                    next_frontier.add(target)
                    sources[target].append((node, transmitted))
                    if target not in hit_time or arrival < hit_time[target]:
                        hit_time[target] = arrival
                    if target not in hop_of or hop < hop_of[target]:
                        hop_of[target] = hop

            if not next_frontier:
                break
            frontier = next_frontier

        exposures: dict[str, NodeExposure] = {}
        for merchant_id in graph.merchant_ids:
            received = incoming.get(merchant_id, 0.0)
            buffer = _buffer_of(graph, merchant_id)
            ratio = received / buffer
            score = _logistic(ratio - 1.0, cfg.logistic_k) if received > 0 else 0.0
            contributors = sorted(
                sources.get(merchant_id, []), key=lambda item: -item[1]
            )[:5]
            edge_lag = _incoming_lag_spread(graph, merchant_id)
            hit = hit_time.get(merchant_id)

            exposures[merchant_id] = NodeExposure(
                merchant_id=merchant_id,
                exposure_score=float(min(1.0, max(0.0, score))),
                expected_shortfall=float(max(0.0, received - buffer)),
                expected_hit_t=hit,
                hit_t_lower=None if hit is None else max(0.0, hit - edge_lag),
                hit_t_upper=None if hit is None else hit + edge_lag,
                hop_distance=hop_of.get(merchant_id),
                contributing_sources=[c for c, _ in contributors],
            )

        return ModelPrediction(
            run_id=run_id,
            shock_id=shock.shock_id,
            predictor=self.kind,
            model_version=model_version,
            horizon_hours=horizon,
            threshold=cfg.threshold,
            exposures=exposures,
            config_hash=config_hash(cfg.to_dict()),
            metadata={"predictor_config": cfg.to_dict()},
        )


class HawkesCascadePredictor:
    """Probabilistic reachability from the excitation parameters alone."""

    kind = PredictorKind.HAWKES_CASCADE

    def __init__(self, config: PropagationConfig | None = None) -> None:
        self.config = config or PropagationConfig()

    def predict(
        self,
        graph: TemporalPaymentGraph,
        shock: Shock,
        *,
        model_version: str = "v1",
        run_id: str | None = None,
    ) -> ModelPrediction:
        cfg = self.config
        horizon = cfg.horizon_hours

        prob: dict[str, float] = dict.fromkeys(graph.merchant_ids, 0.0)
        hit_time: dict[str, float] = {}
        hop_of: dict[str, int] = {}
        sources: dict[str, list[tuple[str, float]]] = defaultdict(list)

        for component in shock.components:
            prob[component.merchant_id] = 1.0
            hit_time[component.merchant_id] = component.t
            hop_of[component.merchant_id] = 0

        for hop in range(1, cfg.max_hops + 1):
            updates: dict[str, float] = {}
            for source in graph.merchant_ids:
                p_source = prob.get(source, 0.0)
                if p_source <= 1e-6:
                    continue
                t_source = hit_time.get(source, 0.0)
                for edge in graph.out_dependencies(source):
                    remaining = horizon - t_source
                    if remaining <= 0:
                        continue
                    # P(transmission occurs) x P(it lands inside the horizon).
                    p_edge = (
                        p_source
                        * edge.conditional_probability
                        * edge.lag.cdf(remaining)
                    )
                    if p_edge <= 1e-6:
                        continue
                    target = edge.target_id
                    # Noisy-OR combination across independent upstream paths.
                    combined = 1.0 - (1.0 - updates.get(target, prob[target])) * (
                        1.0 - p_edge
                    )
                    updates[target] = combined
                    sources[target].append((source, p_edge))

                    arrival = t_source + edge.lag.mean_hours
                    if arrival <= horizon and (
                        target not in hit_time or arrival < hit_time[target]
                    ):
                        hit_time[target] = arrival
                    if target not in hop_of or hop < hop_of[target]:
                        hop_of[target] = hop

            changed = False
            for node, value in updates.items():
                if value > prob.get(node, 0.0) + 1e-9:
                    prob[node] = value
                    changed = True
            if not changed:
                break

        exposures = {
            merchant_id: NodeExposure(
                merchant_id=merchant_id,
                exposure_score=float(min(1.0, max(0.0, prob.get(merchant_id, 0.0)))),
                expected_shortfall=0.0,
                expected_hit_t=hit_time.get(merchant_id),
                hop_distance=hop_of.get(merchant_id),
                contributing_sources=[
                    c for c, _ in sorted(sources.get(merchant_id, []), key=lambda i: -i[1])[:5]
                ],
            )
            for merchant_id in graph.merchant_ids
        }

        return ModelPrediction(
            run_id=run_id,
            shock_id=shock.shock_id,
            predictor=self.kind,
            model_version=model_version,
            horizon_hours=horizon,
            threshold=cfg.threshold,
            exposures=exposures,
            config_hash=config_hash(cfg.to_dict()),
            metadata={"predictor_config": cfg.to_dict()},
        )


def _incoming_lag_spread(graph: TemporalPaymentGraph, merchant_id: str) -> float:
    """Rough +/- band on hit time, from the spread of incoming edge lags."""
    edges = graph.in_dependencies(merchant_id)
    if not edges:
        return 12.0
    spreads = [max(1.0, e.lag.quantile(0.9) - e.lag.quantile(0.1)) / 2.0 for e in edges]
    return float(sum(spreads) / len(spreads))
