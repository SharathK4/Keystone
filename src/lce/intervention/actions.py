"""Stage-1: generating candidate actions, with a reason attached to each.

The search space is combinatorial and every evaluation is a full simulation, so
what goes into the candidate set decides what the whole optimisation costs. This
module builds that set and records *why* each entry is in it.

Two rules govern the ordering of operations, and the order matters:

1. **Feasibility first, score second.** A candidate is discarded if it breaks a
   constraint, before any score is consulted. A model can promote a feasible
   action; it can never make an infeasible one eligible. Inverting this is how
   an optimiser ends up recommending something nobody is allowed to do.

2. **Every scoring factor is measurable.** The score is a documented combination
   of five quantities that exist independently of any model output except the
   calibrated contagion probability, which is itself an observable-only
   prediction. Nothing is selected because "the model liked it".

The factors
-----------
``predicted_downstream_disruption``
    calibrated ``F_i(T)`` times the obligation value sitting downstream of ``i``
    inside the horizon - what breaks, weighted by how likely ``i`` is to break.

``urgency``
    from the predicted time-to-constraint. An intervention that lands after the
    node has already failed is worth nothing, so earlier constraints score
    higher.

``propagation_centrality``
    Katz centrality on the dependency overlay - structural reach, shock-free.

``leverage``
    live obligation value at the node over its own buffer. High leverage means a
    small amount of cash moves a large amount of commerce.

``cost``
    enters negatively, on a log scale, so a cheap action is preferred at equal
    effect without letting a trivially cheap one dominate.

Factors are min-max normalised across the candidate pool before weighting, so
the weights are comparable rather than absorbing the units of whatever they
multiply.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any

import numpy as np

from lce.domain.enums import InterventionType
from lce.domain.events import EXTERNAL_SINK
from lce.domain.intervention import Intervention
from lce.domain.prediction import ModelPrediction
from lce.domain.shock import Shock
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.intervention.problem import InterventionConstraints, check_action
from lce.logging import get_logger
from lce.optimization.candidates import CandidateConfig, generate_candidates

logger = get_logger(__name__)

HOURS_PER_DAY = 24.0

FACTOR_NAMES: tuple[str, ...] = (
    "predicted_downstream_disruption",
    "urgency",
    "propagation_centrality",
    "leverage",
    "cost",
)


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    """Weights on the normalised factors. Declared, not fitted.

    These are a stated preference about what makes a candidate worth simulating,
    and the pruning benchmark measures what they cost: if a weighting throws away
    the exact optimum, that shows up as a drop in optimum-retention rather than
    as a quietly worse result.
    """

    predicted_downstream_disruption: float = 1.0
    urgency: float = 0.5
    propagation_centrality: float = 0.5
    leverage: float = 0.75
    cost: float = -0.25

    def as_vector(self) -> np.ndarray:
        return np.array(
            [
                self.predicted_downstream_disruption,
                self.urgency,
                self.propagation_centrality,
                self.leverage,
                self.cost,
            ],
            dtype=float,
        )

    def to_dict(self) -> dict[str, float]:
        return dict(zip(FACTOR_NAMES, self.as_vector().tolist(), strict=True))


@dataclass(slots=True)
class ScoredAction:
    """One candidate with its factors and the score they produced."""

    intervention: Intervention
    score: float
    factors: dict[str, float] = field(default_factory=dict)
    normalised: dict[str, float] = field(default_factory=dict)

    def explain(self) -> dict[str, Any]:
        """Only measurable factors - no narrative, no model internals."""
        return {
            "intervention_id": self.intervention.intervention_id,
            "type": str(self.intervention.type),
            "merchant_id": self.intervention.merchant_id,
            "description": self.intervention.describe(),
            "cost": self.intervention.cost,
            "score": self.score,
            "factors": dict(self.factors),
            "normalised_factors": dict(self.normalised),
        }


@dataclass(slots=True)
class ActionSet:
    """The pruned candidate set, plus what pruning did."""

    scored: list[ScoredAction] = field(default_factory=list)
    n_generated: int = 0
    n_feasible: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    feasible_pool: list[Intervention] = field(default_factory=list)
    """Every candidate that passed feasibility, before the score-based cut.

    Retained so the pruning benchmark has something to compare against: the
    question "did the filter keep the optimum?" cannot be asked without the set
    the filter was applied to."""

    def __len__(self) -> int:
        return len(self.scored)

    @property
    def interventions(self) -> list[Intervention]:
        return [s.intervention for s in self.scored]

    def top(self, k: int) -> list[Intervention]:
        return self.interventions[:k]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_generated": self.n_generated,
            "n_feasible": self.n_feasible,
            "n_retained": len(self.scored),
            "rejected_by_constraint": dict(self.rejected),
            "weights": self.weights.to_dict(),
            "candidates": [s.explain() for s in self.scored],
        }


def downstream_obligation_value(
    graph: TemporalPaymentGraph, merchant_id: str, horizon: float, *, hops: int = 3
) -> float:
    """Obligation value owed *by* the nodes downstream of ``i``, inside the horizon.

    The quantity at risk if ``i`` stops paying: not ``i``'s own book, but what
    the merchants depending on ``i`` are themselves committed to. A node with no
    downstream reach can still fail, but it cannot start a cascade.
    """
    try:
        downstream = graph.descendants_within(merchant_id, hops)
    except Exception:
        return 0.0
    if not downstream:
        return 0.0
    total = 0.0
    for node in downstream:
        for obligation in graph.payables_of(node):
            if obligation.is_open and obligation.due_t <= horizon:
                total += obligation.outstanding
    return total


def leverage(graph: TemporalPaymentGraph, merchant_id: str, horizon: float) -> float:
    """Live obligation value at the node over its own buffer."""
    profile = graph.merchant(merchant_id)
    live = sum(
        o.outstanding
        for o in graph.payables_of(merchant_id)
        if o.is_open and o.due_t <= horizon
    )
    return live / max(profile.initial_buffer, 1.0)


def _centrality(graph: TemporalPaymentGraph) -> dict[str, float]:
    """Katz centrality on the dependency overlay, empty when there is none."""
    if not graph.dependency_edges:
        return {}
    try:
        return graph.structural_centrality()
    except Exception:
        return {}


def _minmax(values: np.ndarray) -> np.ndarray:
    """Scale to ``[0, 1]``; a constant column becomes zero rather than NaN."""
    lo = float(values.min())
    hi = float(values.max())
    if hi - lo < 1e-12:
        return np.zeros_like(values)
    return (values - lo) / (hi - lo)


def generate_actions(
    graph: TemporalPaymentGraph,
    shock: Shock,
    prediction: ModelPrediction,
    *,
    constraints: InterventionConstraints,
    config: CandidateConfig | None = None,
    weights: ScoringWeights | None = None,
    max_candidates: int = 24,
    benefit_lambda: float = 1.0,
) -> ActionSet:
    """Build, filter and rank the candidate set for one decision.

    The raw proposals come from the Phase-1 generator, unchanged - it already
    knows how to size each action type against a node's predicted shortfall.
    What is added here is the feasibility gate, the explainable score and the
    provenance record.
    """
    cfg = config or CandidateConfig(
        top_k_nodes=8, max_candidates=max(max_candidates * 4, 40)
    )
    raw = generate_candidates(
        graph, shock, prediction, cfg, horizon_hours=constraints.horizon_hours
    )
    weights = weights or ScoringWeights()

    # The Phase-1 generator only proposes actions for nodes the predictor gave a
    # strictly positive exposure score. That misses a whole class of merchant:
    # the objective is value-weighted, so a large, well-capitalised node with an
    # enormous book contributes far more disruption when it slips than a fragile
    # micro merchant does when it fails outright - and its modelled probability
    # can still be near zero. Those nodes were never in the pool to be ranked, so
    # no amount of scoring could recover them. The rule below adds them on a
    # measurable criterion (value at risk inside the horizon), not on a model
    # score.
    proposals = _union(
        raw.interventions,
        coverage_gap_actions(graph, shock, constraints=constraints, top_k=cfg.top_k_nodes),
    )

    feasible: list[Intervention] = []
    rejected: dict[str, int] = {}
    for candidate in proposals:
        report = check_action([candidate], graph, constraints)
        if report.feasible:
            feasible.append(candidate)
            continue
        for name in report.names():
            rejected[name] = rejected.get(name, 0) + 1

    if not feasible:
        logger.info(
            "no_feasible_candidates", n_generated=len(proposals), rejected=rejected
        )
        return ActionSet(
            n_generated=len(proposals),
            n_feasible=0,
            rejected=rejected,
            weights=weights,
        )

    centrality = _centrality(graph)
    horizon = constraints.horizon_hours
    exposures = prediction.exposures

    raw_factors = np.zeros((len(feasible), len(FACTOR_NAMES)), dtype=float)
    for row, candidate in enumerate(feasible):
        merchant_id = candidate.merchant_id
        node = exposures.get(merchant_id)
        probability = node.exposure_score if node is not None else 0.0
        hit = node.expected_hit_t if node is not None else None
        at_risk = downstream_obligation_value(graph, merchant_id, horizon)

        raw_factors[row] = (
            probability * at_risk,
            1.0 / (1.0 + max(0.0, (hit if hit is not None else horizon)) / HOURS_PER_DAY),
            centrality.get(merchant_id, 0.0),
            leverage(graph, merchant_id, horizon),
            math.log1p(max(candidate.cost, 0.0)),
        )

    normalised = np.column_stack([_minmax(raw_factors[:, j]) for j in range(len(FACTOR_NAMES))])
    scores = normalised @ weights.as_vector()

    scored: list[ScoredAction] = []
    for row, candidate in enumerate(feasible):
        factors = dict(zip(FACTOR_NAMES, raw_factors[row].tolist(), strict=True))
        norm = dict(zip(FACTOR_NAMES, normalised[row].tolist(), strict=True))
        stamped = candidate.model_copy(
            update={
                "provenance": {
                    "stage": "candidate_generation",
                    "rule": f"phase1_generator:{candidate.type}",
                    "score": float(scores[row]),
                    "factors": factors,
                    "weights": weights.to_dict(),
                    "sized_from": candidate.provenance.get("sized_from", "predicted_shortfall"),
                }
            }
        )
        scored.append(
            ScoredAction(
                intervention=stamped,
                score=float(scores[row]),
                factors=factors,
                normalised=norm,
            )
        )

    # Two rankings, interleaved - not one weighted sum.
    #
    # The weighted score answers "which node is the model most worried about",
    # and on a value-weighted objective that is not the same question as "which
    # action could prevent the most disruption". A large, well-capitalised
    # merchant can carry a near-zero failure probability and still hold the
    # majority of the value at risk; ranked by the model score alone it is
    # cut, and no amount of re-weighting fixes that without turning the score
    # into the second ranking anyway.
    #
    # So the retained set is drawn alternately from both: the model's ranking,
    # and an upper bound on net benefit measured in rupees. Neither dominates,
    # both are explainable, and the pruning benchmark measures what the merge
    # costs.
    scored.sort(key=lambda s: (-s.score, s.intervention.intervention_id))
    by_benefit = sorted(
        scored,
        key=lambda s: (-_net_benefit_bound(s, lam=benefit_lambda), s.intervention.intervention_id),
    )
    retained = _interleave(
        scored,
        by_benefit,
        limit=max_candidates,
        per_merchant=constraints.max_per_merchant,
    )

    logger.info(
        "actions_generated",
        n_generated=len(proposals),
        n_feasible=len(feasible),
        n_retained=len(retained),
        rejected=rejected,
    )
    return ActionSet(
        scored=retained,
        n_generated=len(proposals),
        n_feasible=len(feasible),
        rejected=rejected,
        weights=weights,
        feasible_pool=[s.intervention for s in scored],
    )


def _net_benefit_bound(action: ScoredAction, *, lam: float) -> float:
    """Upper bound on what an action can be worth, in rupees.

    An action cannot prevent more disruption than the value it protects, so
    ``protected value - lambda * cost`` bounds its contribution to ``J``. Crude,
    and deliberately so: a bound is exactly what a *filter* needs, since it can
    only ever discard actions that could not have won.
    """
    protected = action.factors.get("predicted_downstream_disruption", 0.0)
    leverage_value = action.factors.get("leverage", 0.0)
    return max(protected, leverage_value) - lam * action.intervention.cost


def _interleave(
    primary: Sequence[ScoredAction],
    secondary: Sequence[ScoredAction],
    *,
    limit: int,
    per_merchant: int,
) -> list[ScoredAction]:
    """Take alternately from two rankings, de-duplicated, until ``limit``.

    ``per_merchant`` caps how many candidates one merchant may occupy, and it is
    the constraint that makes the retained set worth its size. The feasible set
    allows at most ``max_per_merchant`` actions on a merchant, so a retention slot
    spent on a second action for a merchant already represented can only ever
    *substitute* for the first - it cannot put a plan in reach that was not
    already. Merchant coverage is what widens the reachable plan space, so the cap
    equals the capacity limit exactly.

    Both parts of that were measured rather than reasoned. Before any cap, twelve
    retained candidates covered five merchants - seven slots were alternatives on
    one node - and the pruning benchmark reported the pool optimum being lost with
    a relative regret of 0.44 to 1.69. A cap of two covered seven merchants and
    still lost it; a cap of one covers eleven or twelve and recovers the optimum
    exactly on one of the two probe networks, halving the regret on the other.
    """
    out: list[ScoredAction] = []
    seen: set[str] = set()
    used: dict[str, int] = {}
    for a, b in zip_longest(primary, secondary):
        for entry in (a, b):
            if entry is None or entry.intervention.intervention_id in seen:
                continue
            merchant = entry.intervention.merchant_id
            if used.get(merchant, 0) >= per_merchant:
                continue
            seen.add(entry.intervention.intervention_id)
            used[merchant] = used.get(merchant, 0) + 1
            out.append(entry)
            if len(out) >= limit:
                return out
    return out


def value_at_risk(
    graph: TemporalPaymentGraph, merchant_id: str, horizon: float
) -> float:
    """Obligation value this merchant must settle inside the horizon.

    The quantity the disruption objective is most sensitive to: every rupee here
    is a rupee that gets value-weighted by its lateness if the merchant cannot
    pay. Independent of any model output.
    """
    return sum(
        o.outstanding
        for o in graph.payables_of(merchant_id)
        if o.is_open and o.due_t <= horizon
    )


def coverage_gap_actions(
    graph: TemporalPaymentGraph,
    shock: Shock,
    *,
    constraints: InterventionConstraints,
    top_k: int = 8,
) -> list[Intervention]:
    """Injections sized to close a merchant's *coverage gap*, for the biggest books.

    The gap is what the merchant is short by if nothing arrives:

        gap_i = horizon payables + direct shock - liquidity buffer

    A merchant with a positive gap cannot clear its book from its own resources,
    and the amount that fixes that is a financial quantity rather than a tuned
    multiple. Candidates are proposed for the merchants with the largest value at
    risk, which is why this rule finds nodes a probability-ranked generator does
    not: a large merchant can be both unlikely to fail and, if it does, by far the
    most expensive failure on the network.
    """
    shock_by_node: dict[str, float] = {}
    for component in shock.components:
        shock_by_node[component.merchant_id] = (
            shock_by_node.get(component.merchant_id, 0.0) + component.magnitude
        )

    horizon = constraints.horizon_hours
    ranked = sorted(
        graph.merchant_ids,
        key=lambda m: (-value_at_risk(graph, m, horizon), m),
    )[:top_k]

    out: list[Intervention] = []
    for merchant_id in ranked:
        exposure = value_at_risk(graph, merchant_id, horizon)
        if exposure <= 0.0:
            continue
        buffer = graph.merchant(merchant_id).initial_buffer
        gap = exposure + shock_by_node.get(merchant_id, 0.0) - buffer
        if gap < constraints.min_amount:
            continue
        out.append(
            Intervention(
                type=InterventionType.LIQUIDITY_INJECTION,
                merchant_id=merchant_id,
                t=constraints.decision_time,
                amount=gap,
                label=f"Close {merchant_id}'s coverage gap of INR {gap:,.0f}",
                provenance={
                    "stage": "candidate_generation",
                    "rule": "coverage_gap",
                    "sized_from": "horizon_payables + shock - buffer",
                    "value_at_risk": exposure,
                    "buffer": buffer,
                },
            )
        )
    return out


def _union(*groups: Sequence[Intervention]) -> list[Intervention]:
    """Concatenate proposal lists, de-duplicated and deterministically ordered."""
    seen: dict[str, Intervention] = {}
    for group in groups:
        for action in group:
            seen.setdefault(action.intervention_id, action)
    return [seen[k] for k in sorted(seen)]


# ------------------------------------------------------------------ baselines


def standard_injection(
    graph: TemporalPaymentGraph,
    merchant_id: str,
    *,
    constraints: InterventionConstraints,
    t: float,
    rule: str,
    fraction_of_payables: float = 1.0,
) -> Intervention | None:
    """A standardised injection at ``merchant_id``, or ``None`` if infeasible.

    Used by the naive baselines so they differ from the model-guided procedure in
    *whom they pick* and nothing else. Giving each baseline its own sizing rule
    would confound the comparison: the question is whether the selection is
    better, not whether the cheque is.
    """
    if not graph.has_merchant(merchant_id):
        return None
    payables = sum(
        o.outstanding
        for o in graph.payables_of(merchant_id)
        if o.is_open and o.due_t <= constraints.horizon_hours
    )
    amount = max(payables * fraction_of_payables, constraints.min_amount)
    candidate = Intervention(
        type=InterventionType.LIQUIDITY_INJECTION,
        merchant_id=merchant_id,
        t=t,
        amount=amount,
        label=f"Inject INR {amount:,.0f} into {merchant_id} ({rule})",
        provenance={"stage": "baseline", "rule": rule, "sized_from": "horizon_payables"},
    )
    return candidate if check_action([candidate], graph, constraints).feasible else None


def rank_by_open_deficit(graph: TemporalPaymentGraph, horizon: float) -> list[str]:
    """Merchants ranked by how far their horizon payables exceed their buffer.

    The "largest deficit" naive rule: help whoever is most obviously short. It
    ignores the network entirely, which is exactly what makes it a useful
    control.
    """
    scored = []
    for merchant_id in graph.merchant_ids:
        payables = sum(
            o.outstanding
            for o in graph.payables_of(merchant_id)
            if o.is_open and o.due_t <= horizon
        )
        scored.append((merchant_id, payables - graph.merchant(merchant_id).initial_buffer))
    return [m for m, _ in sorted(scored, key=lambda kv: (-kv[1], kv[0]))]


def rank_by_degree(graph: TemporalPaymentGraph) -> list[str]:
    """Merchants ranked by observed transaction degree - the structural control."""
    degree: dict[str, int] = dict.fromkeys(graph.merchant_ids, 0)
    for payer, payee in graph.distinct_pairs():
        if EXTERNAL_SINK in (payer, payee):
            continue
        if payer in degree:
            degree[payer] += 1
        if payee in degree:
            degree[payee] += 1
    return [m for m, _ in sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))]


def rank_by_cash_cover(graph: TemporalPaymentGraph, horizon: float) -> list[str]:
    """Merchants ranked by worst cover ratio: payables due over buffer plus receivables.

    The treasurer's arithmetic from Phase 3, reused as an intervention rule so
    the two phases are answering the same question with the same heuristic.
    """
    scored = []
    for merchant_id in graph.merchant_ids:
        profile = graph.merchant(merchant_id)
        payables = sum(
            o.outstanding
            for o in graph.payables_of(merchant_id)
            if o.is_open and o.due_t <= horizon
        )
        receivables = sum(
            o.outstanding
            for o in graph.receivables_of(merchant_id)
            if o.is_open and o.due_t <= horizon
        )
        cover = (profile.initial_buffer + receivables) / max(payables, 1.0)
        scored.append((merchant_id, cover))
    return [m for m, _ in sorted(scored, key=lambda kv: (kv[1], kv[0]))]


def baseline_actions(
    graph: TemporalPaymentGraph,
    ranking: Sequence[str],
    *,
    constraints: InterventionConstraints,
    t: float,
    rule: str,
    max_actions: int | None = None,
) -> list[Intervention]:
    """Take the top of a ranking and turn it into feasible standardised actions."""
    limit = max_actions if max_actions is not None else constraints.max_actions
    out: list[Intervention] = []
    for merchant_id in ranking:
        if len(out) >= limit:
            break
        action = standard_injection(
            graph, merchant_id, constraints=constraints, t=t, rule=rule
        )
        if action is not None:
            out.append(action)
    return out
