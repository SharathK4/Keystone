"""Reproducible benchmark shock scenarios.

Seven families, each parameterised by network, magnitude, timing and seed, and
each identified by a content-addressed ``scenario_id`` so a result can always be
traced back to the exact perturbation that produced it.

Why some families are graph mutations rather than shocks
--------------------------------------------------------
The simulator's :class:`~lce.domain.shock.Shock` vector removes *cash*. Two of
the required families are not cash removals:

* **delayed inflow** - the receivable still arrives, later. That is a change to
  an obligation's deadline, not a withdrawal.
* **supplier failure** - a counterparty stops paying at all, so its obligations
  must be written off rather than merely reduced.

Rather than extend the simulator's shock vocabulary (and with it the propagation
model, which is out of scope here), a scenario may carry **obligation mutations**
that are applied to a *copy* of the graph before the run. The engine is untouched
and the semantics stay explicit: a scenario is `(mutated network, shock)`.

Every family also records the exact obligation ids it touched, so the ground
truth can state what was perturbed without the model ever seeing it.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from lce.benchmark.manifest import make_scenario_id
from lce.domain.enums import ObligationStatus, ShockKind
from lce.domain.events import EXTERNAL_SINK, Obligation
from lce.domain.shock import Shock, ShockComponent
from lce.errors import ValidationError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.seeds import derive_seed

HOURS_PER_DAY = 24.0


class ScenarioFamily(StrEnum):
    """The benchmark's shock taxonomy."""

    SINGLE_MISSED_INFLOW = "single_missed_inflow"
    DELAYED_INFLOW = "delayed_inflow"
    PARTIAL_PAYMENT = "partial_payment"
    LIQUIDITY_DRAIN = "liquidity_drain"
    SUPPLIER_FAILURE = "supplier_failure"
    CONCENTRATED_SHOCK = "concentrated_shock"
    MULTI_NODE_SHOCK = "multi_node_shock"


class TargetStrategy(StrEnum):
    """How the shocked merchant(s) are chosen."""

    MOST_CONNECTED = "most_connected"  # largest downstream reach - the bottleneck
    MOST_DEPENDENT = "most_dependent"  # largest inbound receivable relative to buffer
    LARGEST = "largest"                # biggest balance sheet
    RANDOM = "random"
    EXPLICIT = "explicit"


# Families that perturb a single inbound receivable need a target for whom that
# receivable is actually material. Aiming them at the most *connected* node picks
# a well-capitalised hub whose largest inflow is a rounding error against its
# buffer, and the scenario then measures nothing. Each family therefore declares
# the selection rule that makes it bite.
DEFAULT_STRATEGY_FOR_FAMILY: dict[ScenarioFamily, TargetStrategy] = {
    ScenarioFamily.SINGLE_MISSED_INFLOW: TargetStrategy.MOST_DEPENDENT,
    ScenarioFamily.DELAYED_INFLOW: TargetStrategy.MOST_DEPENDENT,
    ScenarioFamily.PARTIAL_PAYMENT: TargetStrategy.MOST_DEPENDENT,
    ScenarioFamily.LIQUIDITY_DRAIN: TargetStrategy.MOST_CONNECTED,
    ScenarioFamily.SUPPLIER_FAILURE: TargetStrategy.MOST_CONNECTED,
    ScenarioFamily.CONCENTRATED_SHOCK: TargetStrategy.MOST_CONNECTED,
    ScenarioFamily.MULTI_NODE_SHOCK: TargetStrategy.MOST_CONNECTED,
}


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    """A reproducible description of one benchmark perturbation."""

    family: ScenarioFamily
    magnitude: float = 2.0
    """Severity. For cash families, a multiple of the target's liquidity
    *slack* - buffer plus net expected inflow - not of its buffer alone. At 1.0
    the cushion is exactly exhausted; above 1.0 the merchant cannot recover
    within the horizon. See :func:`liquidity_slack`."""
    shock_time: float | None = None
    """When the shock lands. ``None`` places it where it actually bites.

    Timing is not cosmetic. Draining a merchant at ``t=0`` is usually absorbed:
    its own receivables arrive over the following days and restore the buffer
    long before its payables fall due, so the cascade never starts. The default
    resolves to the moment the cash is *needed* - immediately before the
    merchant's first commitment, or the instant an expected inflow fails to
    arrive. See :func:`resolve_shock_time`.
    """
    delay_hours: float = 120.0
    """Deadline shift for DELAYED_INFLOW.

    Five days. A shorter slip catches too few of the merchant's commitments to
    leave any of them unfunded on a weekly horizon, so the family builds but
    reliably measures nothing; 120h is both realistic for a distressed payer and
    long enough to bite across seeds."""
    partial_fraction: float = 0.85
    """Share of the expected inflow that fails to arrive, for PARTIAL_PAYMENT.

    Defaults high because a distressed customer typically remits a token amount
    rather than half. At 0.5 the family is close to vacuous on a realistically
    capitalised network: losing half of one invoice is comfortably inside most
    merchants' slack, so the scenario builds but nothing ever breaks."""
    n_targets: int = 3
    """Number of shocked nodes for MULTI_NODE_SHOCK."""
    target_strategy: TargetStrategy = TargetStrategy.MOST_CONNECTED
    explicit_targets: tuple[str, ...] = ()
    seed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": str(self.family),
            "magnitude": self.magnitude,
            "shock_time": self.shock_time,
            "delay_hours": self.delay_hours,
            "partial_fraction": self.partial_fraction,
            "n_targets": self.n_targets,
            "target_strategy": str(self.target_strategy),
            "explicit_targets": list(self.explicit_targets),
            "seed": self.seed,
        }

    def scenario_id(self, dataset_id: str) -> str:
        return make_scenario_id(dataset_id, self.to_dict())


@dataclass(slots=True)
class ObligationMutation:
    """A recorded change to one obligation, applied before the run."""

    obligation_id: str
    kind: str
    debtor_id: str
    creditor_id: str
    before: dict[str, Any]
    after: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "kind": self.kind,
            "debtor_id": self.debtor_id,
            "creditor_id": self.creditor_id,
            "before": self.before,
            "after": self.after,
        }


@dataclass(slots=True)
class BuiltScenario:
    """A scenario materialised against a concrete network."""

    scenario_id: str
    spec: ScenarioSpec
    dataset_id: str
    graph: TemporalPaymentGraph
    """The (possibly mutated) network the run must use."""
    shock: Shock
    targets: list[str]
    mutations: list[ObligationMutation] = field(default_factory=list)
    baseline_graph: TemporalPaymentGraph | None = None
    """The network *before* this scenario's mutations.

    Families like ``delayed_inflow`` and ``supplier_failure`` express the shock
    as a change to the obligation book rather than as a cash withdrawal. Their
    counterfactual baseline must therefore be run on the unmutated network -
    running it on ``graph`` would bake the perturbation into the baseline too,
    and the attributable set (shocked minus baseline) would come out empty no
    matter how damaging the scenario actually was.
    """

    @property
    def unperturbed_graph(self) -> TemporalPaymentGraph:
        """The network the no-shock baseline must be simulated on."""
        return self.baseline_graph if self.baseline_graph is not None else self.graph

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "dataset_id": self.dataset_id,
            "family": str(self.spec.family),
            "spec": self.spec.to_dict(),
            "targets": self.targets,
            "shock_id": self.shock.shock_id,
            "shock_magnitude": self.shock.total_magnitude,
            "n_mutations": len(self.mutations),
            "mutations": [m.to_dict() for m in self.mutations],
        }


# --------------------------------------------------------------- target choice


def baseline_affected_set(
    graph: TemporalPaymentGraph, config: Any | None = None
) -> set[str]:
    """Merchants that already fail with no shock applied.

    Scenario authoring is allowed to consult the simulator - the *model* never
    sees this, only the benchmark author does. It is needed because ground truth
    is defined as ``affected(shock) \\ affected(baseline)``: a node that was
    already failing cannot be counted as shock-attributable, so aiming a shock
    at one produces a scenario that measures nothing.
    """
    from lce.simulation.engine import LiquiditySimulator, SimulationConfig

    sim_config = config or SimulationConfig()
    result = LiquiditySimulator(graph, sim_config).run(None, run_id="scenario:baseline")
    return set(result.affected_ids)


def horizon_end(graph: TemporalPaymentGraph, fallback: float = 168.0) -> float:
    """Latest scheduled deadline in the network - a proxy for the horizon."""
    dues = [o.due_t for o in graph.obligations if o.due_t >= 0.0]
    return max(dues) if dues else fallback


def _commitments_in_window(
    graph: TemporalPaymentGraph, merchant_id: str, start_t: float, window_hours: float
) -> float:
    """Payables falling due in ``(start_t, start_t + window]``."""
    cutoff = start_t + window_hours
    return sum(
        o.outstanding
        for o in graph.payables_of(merchant_id)
        if o.is_open and start_t <= o.due_t <= cutoff
    )


def _severity_profile(spec: ScenarioSpec) -> tuple[float, float]:
    """``(fraction_of_inflow_lost, hours_it_is_unavailable)`` for a family.

    Used when choosing a target so that each receivable family is aimed at a
    merchant its *own* severity is enough to break.
    """
    match spec.family:
        case ScenarioFamily.PARTIAL_PAYMENT:
            return spec.partial_fraction, float("inf")
        case ScenarioFamily.DELAYED_INFLOW:
            # The money is not lost, only late - so only commitments inside the
            # delay window are actually left unfunded.
            return 1.0, spec.delay_hours
        case _:
            return 1.0, float("inf")


def select_targets(
    graph: TemporalPaymentGraph,
    spec: ScenarioSpec,
    *,
    count: int = 1,
    exclude: Collection[str] = (),
) -> list[str]:
    """Pick the merchant(s) to perturb, deterministically for a given seed.

    ``exclude`` removes merchants that are unusable as targets - in practice the
    ones already failing in the baseline, whose damage cannot be attributed to
    the shock. Explicit targets bypass the filter: if a caller names a node, it
    gets that node.
    """
    if spec.target_strategy is TargetStrategy.EXPLICIT:
        unknown = [m for m in spec.explicit_targets if not graph.has_merchant(m)]
        if unknown:
            raise ValidationError(f"unknown explicit targets: {unknown}")
        if not spec.explicit_targets:
            raise ValidationError("EXPLICIT target strategy requires explicit_targets")
        return list(spec.explicit_targets[:count])

    metrics = network_metrics(graph)
    blocked = set(exclude)
    candidates = [
        m
        for m in sorted(graph.merchant_ids)
        if graph.out_dependencies(m) and m not in blocked
    ]
    if not candidates:
        # Every node with downstream reach is already distressed. Fall back to
        # the unfiltered set rather than failing: a saturated network is a valid
        # (if uninformative) benchmark case, and the ground truth will show it.
        candidates = [m for m in sorted(graph.merchant_ids) if m not in blocked]
    if not candidates:
        candidates = sorted(graph.merchant_ids)
    if not candidates:
        raise ValidationError("cannot build a scenario on an empty network")

    match spec.target_strategy:
        case TargetStrategy.MOST_CONNECTED:
            # Ranked by criticality rather than raw reach: the largest hub is
            # usually the best-capitalised one, and draining a multiple of its
            # buffer still leaves it solvent once its own inflows arrive.
            ranked = sorted(candidates, key=lambda m: (-metrics.criticality(m), m))
        case TargetStrategy.MOST_DEPENDENT:
            # Fragile *and* load-bearing, and holding a receivable whose loss
            # actually leaves commitments unfunded.
            #
            # The severity is family-specific and must be applied here, not just
            # when the shock is built. A write-off removes the whole inflow, a
            # partial payment removes a fraction of it, and a delay withholds it
            # only for the delay window. Sharing one target across all three
            # tunes it for the harshest family and leaves the gentler ones
            # landing on a merchant with enough slack to shrug them off.
            fraction, window = _severity_profile(spec)
            scored: list[tuple[float, float, str]] = []
            for merchant_id in candidates:
                inbound = [
                    o
                    for o in graph.receivables_of(merchant_id)
                    if o.is_open and o.debtor_id != EXTERNAL_SINK
                ]
                if not inbound:
                    continue
                at_stake = max(
                    min(
                        o.outstanding * fraction,
                        _commitments_in_window(graph, merchant_id, o.due_t, window),
                    )
                    for o in inbound
                )
                if at_stake <= 0:
                    continue
                slack = liquidity_slack(graph, merchant_id)
                reach = len(metrics.reach.get(merchant_id, ()))
                scored.append((at_stake / slack, float(reach), merchant_id))
            if not scored:
                raise ValidationError(
                    "no merchant has a load-bearing inbound receivable to perturb"
                )
            # Vulnerability is a gate, not a score to trade off against reach.
            # A merchant whose exposure exceeds its slack *will* break; among
            # those, the useful target is the one with the most downstream, since
            # that is where a cascade can actually travel. Multiplying the two
            # instead lets an extremely exposed leaf outrank a merely-broken hub,
            # producing scenarios with one victim and no propagation.
            vulnerable = [row for row in scored if row[0] >= 1.0]
            pool = vulnerable or scored
            ranked = [
                m
                for _, _, m in sorted(
                    pool,
                    key=lambda kv: (-kv[1], -kv[0], kv[2])
                    if vulnerable
                    else (-kv[0], -kv[1], kv[2]),
                )
            ]
        case TargetStrategy.LARGEST:
            ranked = sorted(
                candidates, key=lambda m: (-graph.merchant(m).initial_buffer, m)
            )
        case TargetStrategy.RANDOM:
            rng = np.random.default_rng(derive_seed(spec.seed, "targets"))
            ranked = list(rng.permutation(candidates))
        case _:  # pragma: no cover - exhaustive above
            ranked = candidates
    return [str(m) for m in ranked[:count]]


def horizon_payables(
    graph: TemporalPaymentGraph, merchant_id: str, *, after_t: float = 0.0
) -> float:
    """Cash the merchant is committed to pay out after ``after_t``."""
    return sum(
        o.outstanding
        for o in graph.payables_of(merchant_id)
        if o.is_open and o.due_t >= after_t
    )


def liquidity_slack(
    graph: TemporalPaymentGraph, merchant_id: str, *, horizon_hours: float = 168.0
) -> float:
    """Total cushion a merchant can absorb a shock with, over the horizon.

    Not just the opening buffer. A merchant whose receivables exceed its
    payables is *replenished* during the horizon, and a drain sized against its
    buffer alone is simply refilled before any deadline arrives - which is why
    an apparently severe "2x buffer" shock can leave the affected set empty.

    Slack is therefore ``buffer + (receivables - payables) + net exogenous
    flow``, floored at a small positive value so the caller always gets a usable
    scale even for a merchant with no commitments.
    """
    profile = graph.merchant(merchant_id)
    receivables = sum(o.outstanding for o in graph.receivables_of(merchant_id) if o.is_open)
    payables = horizon_payables(graph, merchant_id)
    exogenous = (
        profile.exogenous_inflow_rate - profile.operating_burn_rate
    ) * horizon_hours
    slack = profile.initial_buffer + (receivables - payables) + exogenous
    return max(slack, profile.initial_buffer * 0.25, 1.0)


def fragility(graph: TemporalPaymentGraph, merchant_id: str) -> float:
    """How close a merchant is to being unable to meet its commitments.

    ``payables / buffer``. Above 1 the node cannot clear its book from its own
    resources and must be paid by its buyers to survive - which is exactly the
    condition under which a shock propagates rather than being absorbed.
    """
    buffer = max(graph.merchant(merchant_id).initial_buffer, 1.0)
    return horizon_payables(graph, merchant_id) / buffer


#: Per-node cap on fragility when scoring downstream exposure, so one extremely
#: stretched dependent cannot dominate the sum.
_MAX_DOWNSTREAM_FRAGILITY = 5.0


@dataclass(slots=True)
class NetworkMetrics:
    """Per-merchant reach and fragility, computed in a single pass.

    Scoring every candidate individually is quadratic in a way that bites hard:
    ``descendants_within`` rebuilds the dependency ``DiGraph`` on *each* call, so
    ranking 1,000 candidates rebuilt a 2,600-edge graph 1,000 times and
    recomputed the same fragilities repeatedly. Building the graph once and
    memoising fragility turns target selection from minutes into a moment at
    MEDIUM scale, with identical results.
    """

    reach: dict[str, set[str]]
    fragility: dict[str, float]

    def criticality(self, merchant_id: str) -> float:
        downstream = self.reach.get(merchant_id, set())
        if not downstream:
            return 0.0
        downstream_fragility = sum(
            min(self.fragility.get(node, 0.0), _MAX_DOWNSTREAM_FRAGILITY)
            for node in downstream
        )
        return self.fragility.get(merchant_id, 0.0) * math.log1p(downstream_fragility)


def network_metrics(
    graph: TemporalPaymentGraph, *, hops: int = 3
) -> NetworkMetrics:
    """Reach and fragility for every merchant, from one dependency graph build."""
    import networkx as nx

    dg = graph.dependency_graph()
    reach: dict[str, set[str]] = {}
    for merchant_id in graph.merchant_ids:
        if merchant_id not in dg:
            reach[merchant_id] = set()
            continue
        found = nx.single_source_shortest_path_length(dg, merchant_id, cutoff=hops)
        reach[merchant_id] = {n for n in found if n != merchant_id}
    return NetworkMetrics(
        reach=reach,
        fragility={m: fragility(graph, m) for m in graph.merchant_ids},
    )


def criticality(graph: TemporalPaymentGraph, merchant_id: str, *, hops: int = 3) -> float:
    """Rank targets by where a cascade can actually travel.

    Three things have to hold before a shock produces a multi-hop cascade:

    * the node is **fragile** - its commitments exceed its own slack, so losing
      an inflow breaks it rather than being absorbed;
    * it is **load-bearing** - merchants sit downstream of it;
    * those downstream merchants are **themselves fragile** - otherwise the
      failure stops at the first ring, because well-capitalised dependents
      absorb the missed payment and the cascade has depth zero.

    The third factor is the one that is easy to omit and expensive to omit.
    Scoring on the node's own fragility and raw reach picks a stretched hub
    surrounded by comfortable suppliers, which reliably yields exactly one
    victim. Weighting reach by downstream fragility instead finds the chains
    that propagate.
    """
    downstream = graph.descendants_within(merchant_id, hops)
    if not downstream:
        return 0.0
    downstream_fragility = sum(
        min(fragility(graph, node), _MAX_DOWNSTREAM_FRAGILITY) for node in downstream
    )
    return fragility(graph, merchant_id) * math.log1p(downstream_fragility)


#: Hours before a commitment falls due that a cash shock is placed. Small, so
#: the merchant has no time to be replenished before it must pay.
SHOCK_LEAD_HOURS = 1.0


def resolve_shock_time(
    graph: TemporalPaymentGraph,
    merchant_id: str,
    *,
    explicit: float | None,
    receivable: Obligation | None = None,
) -> float:
    """When a shock should land so that it is economically meaningful.

    An explicit time always wins. Otherwise:

    * with a named receivable, the shock lands at that obligation's **due date** -
      the moment the money was expected and did not arrive;
    * otherwise it lands just before the merchant's **first commitment** in the
      horizon, so every payable it owes is still ahead of it and the drained
      cash is genuinely unavailable when needed.

    Placing cash shocks at ``t=0`` instead lets the merchant rebuild its balance
    from inbound payments before any deadline arrives, which is why an
    apparently severe drain can leave the affected set empty.
    """
    if explicit is not None:
        return explicit
    if receivable is not None:
        return max(0.0, receivable.due_t)

    upcoming = [
        o.due_t for o in graph.payables_of(merchant_id) if o.is_open and o.due_t >= 0.0
    ]
    if not upcoming:
        return 0.0
    return max(0.0, min(upcoming) - SHOCK_LEAD_HOURS)


def _load_bearing_receivable(
    graph: TemporalPaymentGraph,
    merchant_id: str,
    after_t: float,
    *,
    window_hours: float = float("inf"),
    fraction: float = 1.0,
    healthy_debtors: Collection[str] | None = None,
) -> Obligation:
    """The inbound payment this merchant most depends on to meet its own bills.

    Selecting simply the *largest* receivable is wrong: the largest one is often
    due near the end of the horizon, so removing it changes nothing inside the
    simulated window. What matters is how much of the merchant's *subsequent*
    payables that inflow funds - an inflow with no commitments behind it is not
    load-bearing, however big it is.
    """
    options = [
        o
        for o in graph.receivables_of(merchant_id)
        if o.is_open and o.due_t >= after_t and o.debtor_id != EXTERNAL_SINK
    ]
    # Prefer an inflow that would actually have arrived. Writing off a payment
    # from a debtor that defaults in the baseline anyway changes nothing: the
    # creditor never received it either way, so the scenario registers no
    # attributable damage however large the sum looks.
    if healthy_debtors is not None:
        solvent = [o for o in options if o.debtor_id in healthy_debtors]
        if solvent:
            options = solvent
    if not options:
        raise ValidationError(
            f"merchant {merchant_id!r} has no open receivable due at or after "
            f"t={after_t}; this family needs one to perturb",
            merchant_id=merchant_id,
        )

    def score(o: Obligation) -> tuple[float, float, str]:
        # Cash actually at stake: what the inflow covers of the commitments that
        # fall inside the affected window. For a write-off that window is the
        # rest of the horizon; for a *delay* it is only the delay itself, since
        # commitments after the money finally lands are still funded.
        downstream_commitments = _commitments_in_window(
            graph, merchant_id, o.due_t, window_hours
        )
        at_stake = min(o.outstanding * fraction, downstream_commitments)
        return (at_stake, o.outstanding, o.obligation_id)

    best = max(options, key=score)
    if score(best)[0] <= 0.0:
        # Nothing is due after any receivable - fall back to the largest so the
        # family still builds, and let the ground truth report the null result.
        return max(options, key=lambda o: (o.outstanding, -o.due_t, o.obligation_id))
    return best


# ------------------------------------------------------------------- families


def build_scenario(
    graph: TemporalPaymentGraph,
    spec: ScenarioSpec,
    *,
    dataset_id: str,
    baseline_affected: Collection[str] | None = None,
) -> BuiltScenario:
    """Materialise a scenario against a network.

    The returned graph is always a **copy**: a scenario must never mutate the
    dataset it was built from, or the next scenario would silently inherit the
    previous one's damage.
    """
    working = graph.copy()
    pristine = graph.copy()
    scenario_id = spec.scenario_id(dataset_id)
    mutations: list[ObligationMutation] = []
    exclude = set(baseline_affected or ())
    # Debtors that meet their commitments without a shock; only their
    # payments are worth perturbing.
    healthy = (
        {m for m in working.merchant_ids if m not in exclude}
        if baseline_affected is not None
        else None
    )

    match spec.family:
        case ScenarioFamily.SINGLE_MISSED_INFLOW:
            targets = select_targets(working, spec, count=1, exclude=exclude)
            target = targets[0]
            fraction, window = _severity_profile(spec)
            receivable = _load_bearing_receivable(
                working,
                target,
                0.0,
                window_hours=window,
                fraction=fraction,
                healthy_debtors=healthy,
            )
            shock_t = resolve_shock_time(
                working, target, explicit=spec.shock_time, receivable=receivable
            )
            shock = Shock(
                name=f"{spec.family}:{target}",
                description=(
                    f"{target} never receives INR {receivable.outstanding:,.0f} "
                    f"from {receivable.debtor_id} (was due t={receivable.due_t:.0f}h)"
                ),
                components=[
                    ShockComponent(
                        merchant_id=target,
                        magnitude=max(receivable.outstanding, 1e-6),
                        t=shock_t,
                        kind=ShockKind.MISSED_INBOUND,
                        target_obligation_id=receivable.obligation_id,
                    )
                ],
            )

        case ScenarioFamily.DELAYED_INFLOW:
            # Not a cash removal: the money still comes, just late. Expressed as
            # a deadline shift on the inbound obligation, with an empty shock.
            targets = select_targets(working, spec, count=1, exclude=exclude)
            target = targets[0]
            fraction, window = _severity_profile(spec)
            receivable = _load_bearing_receivable(
                working,
                target,
                0.0,
                window_hours=window,
                fraction=fraction,
                healthy_debtors=healthy,
            )
            shock_t = resolve_shock_time(
                working, target, explicit=spec.shock_time, receivable=receivable
            )
            # Capped inside the horizon. Pushing a deadline past the end of the
            # measured window makes the obligation vanish from the accounting
            # entirely - the debtor is never charged for it - so a delay would
            # register as a *relief* that cancels the creditor's loss. A payment
            # deferred beyond the window is also indistinguishable from a
            # write-off, which is what SINGLE_MISSED_INFLOW already models.
            horizon = horizon_end(working)
            new_due = min(receivable.due_t + spec.delay_hours, horizon - 1.0)
            if new_due <= receivable.due_t:
                raise ValidationError(
                    f"cannot delay {receivable.obligation_id!r}: it is already due "
                    f"at the end of the horizon"
                )
            working.add_obligation(receivable.with_deadline(new_due))
            mutations.append(
                ObligationMutation(
                    obligation_id=receivable.obligation_id,
                    kind="deadline_shift",
                    debtor_id=receivable.debtor_id,
                    creditor_id=receivable.creditor_id,
                    before={"due_t": receivable.due_t},
                    after={"due_t": new_due},
                )
            )
            # A tiny book-keeping shock keeps the scenario's shock id stable and
            # marks the origin node; magnitude is negligible next to the delay.
            shock = Shock(
                name=f"{spec.family}:{target}",
                description=(
                    f"{target}'s inflow of INR {receivable.outstanding:,.0f} is "
                    f"delayed by {spec.delay_hours:.0f}h"
                ),
                components=[
                    ShockComponent(
                        merchant_id=target,
                        magnitude=1.0,
                        t=shock_t,
                        kind=ShockKind.MISSED_INBOUND,
                    )
                ],
            )

        case ScenarioFamily.PARTIAL_PAYMENT:
            targets = select_targets(working, spec, count=1, exclude=exclude)
            target = targets[0]
            fraction, window = _severity_profile(spec)
            receivable = _load_bearing_receivable(
                working,
                target,
                0.0,
                window_hours=window,
                fraction=fraction,
                healthy_debtors=healthy,
            )
            shortfall = max(receivable.outstanding * spec.partial_fraction, 1.0)
            shock_t = resolve_shock_time(
                working, target, explicit=spec.shock_time, receivable=receivable
            )
            shock = Shock(
                name=f"{spec.family}:{target}",
                description=(
                    f"{target} receives only "
                    f"{(1 - spec.partial_fraction) * 100:.0f}% of an expected "
                    f"INR {receivable.outstanding:,.0f}"
                ),
                components=[
                    ShockComponent(
                        merchant_id=target,
                        magnitude=shortfall,
                        t=shock_t,
                        kind=ShockKind.MISSED_INBOUND,
                    )
                ],
            )

        case ScenarioFamily.LIQUIDITY_DRAIN:
            targets = select_targets(working, spec, count=1, exclude=exclude)
            target = targets[0]
            magnitude = max(
                liquidity_slack(working, target) * spec.magnitude, 1.0
            )
            shock_t = resolve_shock_time(working, target, explicit=spec.shock_time)
            shock = Shock(
                name=f"{spec.family}:{target}",
                description=(
                    f"{target} loses INR {magnitude:,.0f} of cash "
                    f"({spec.magnitude:g}x its buffer)"
                ),
                components=[
                    ShockComponent(
                        merchant_id=target,
                        magnitude=magnitude,
                        t=shock_t,
                        kind=ShockKind.CASH_WITHDRAWAL,
                    )
                ],
            )

        case ScenarioFamily.SUPPLIER_FAILURE:
            # The supplier stops paying entirely: every payable it owes is
            # written off, and it is drained so it cannot recover.
            targets = select_targets(working, spec, count=1, exclude=exclude)
            target = targets[0]
            # Resolved before the write-off loop: cancelling every payable would
            # leave no commitment to place the shock against.
            shock_t = resolve_shock_time(working, target, explicit=spec.shock_time)
            for obligation in working.payables_of(target):
                if not obligation.is_open:
                    continue
                working.add_obligation(
                    obligation.model_copy(update={"status": ObligationStatus.CANCELLED})
                )
                mutations.append(
                    ObligationMutation(
                        obligation_id=obligation.obligation_id,
                        kind="written_off",
                        debtor_id=obligation.debtor_id,
                        creditor_id=obligation.creditor_id,
                        before={"status": str(obligation.status)},
                        after={"status": str(ObligationStatus.CANCELLED)},
                    )
                )
            magnitude = max(
                liquidity_slack(working, target) * max(spec.magnitude, 2.0), 1.0
            )
            shock = Shock(
                name=f"{spec.family}:{target}",
                description=(
                    f"supplier {target} fails: {len(mutations)} payables written "
                    f"off and INR {magnitude:,.0f} drained"
                ),
                components=[
                    ShockComponent(
                        merchant_id=target,
                        magnitude=magnitude,
                        t=shock_t,
                        kind=ShockKind.CASH_WITHDRAWAL,
                    )
                ],
            )

        case ScenarioFamily.CONCENTRATED_SHOCK:
            # Deliberately aimed at the structural bottleneck, whatever the
            # requested strategy: this family exists to test the worst case.
            concentrated = network_metrics(working, hops=4)
            ranked = sorted(
                (
                    m
                    for m in working.merchant_ids
                    if working.out_dependencies(m) and m not in exclude
                ),
                key=lambda m: (-concentrated.criticality(m), m),
            )
            if not ranked:
                ranked = sorted(
                    m for m in working.merchant_ids if working.out_dependencies(m)
                )
            if not ranked:
                raise ValidationError("network has no node with downstream reach")
            target = ranked[0]
            targets = [target]
            magnitude = max(
                liquidity_slack(working, target) * spec.magnitude, 1.0
            )
            shock_t = resolve_shock_time(working, target, explicit=spec.shock_time)
            shock = Shock(
                name=f"{spec.family}:{target}",
                description=(
                    f"concentrated shock on bottleneck {target} "
                    f"({len(working.descendants_within(target, 4))} nodes downstream)"
                ),
                components=[
                    ShockComponent(
                        merchant_id=target,
                        magnitude=magnitude,
                        t=shock_t,
                        kind=ShockKind.CASH_WITHDRAWAL,
                    )
                ],
            )

        case ScenarioFamily.MULTI_NODE_SHOCK:
            targets = select_targets(working, spec, count=max(1, spec.n_targets), exclude=exclude)
            components = [
                ShockComponent(
                    merchant_id=m,
                    magnitude=max(liquidity_slack(working, m) * spec.magnitude, 1.0),
                    t=resolve_shock_time(working, m, explicit=spec.shock_time),
                    kind=ShockKind.CASH_WITHDRAWAL,
                )
                for m in targets
            ]
            shock = Shock(
                name=f"{spec.family}:{len(targets)}",
                description=f"simultaneous shock across {len(targets)} merchants",
                components=components,
            )

        case _:  # pragma: no cover - match is exhaustive over the enum
            raise ValidationError(f"unsupported scenario family {spec.family!r}")

    return BuiltScenario(
        scenario_id=scenario_id,
        spec=spec,
        dataset_id=dataset_id,
        graph=working,
        shock=shock,
        targets=targets,
        mutations=mutations,
        baseline_graph=pristine,
    )


def scenario_suite(
    graph: TemporalPaymentGraph,
    *,
    dataset_id: str,
    seed: int = 0,
    magnitude: float = 2.0,
    shock_time: float | None = None,
    families: tuple[ScenarioFamily, ...] | None = None,
    baseline_affected: Collection[str] | None = None,
    config: Any | None = None,
) -> list[BuiltScenario]:
    """One scenario per family - the standard benchmark sweep.

    The no-shock baseline is computed once (unless supplied) and every family
    targets a merchant that is *healthy* in it. Without that filter the
    receivable-driven families reliably pick the most cash-strapped node in the
    network - the one whose incoming payment dwarfs its buffer - which is
    exactly the node already failing without any shock, so its damage is not
    attributable and the scenario measures nothing.

    Families that cannot be built on a given network (for instance, no healthy
    merchant has an open receivable to miss) are skipped rather than raising, so
    a suite on a sparse network still returns everything it *can* measure.
    """
    chosen = families or tuple(ScenarioFamily)
    already_failing = (
        set(baseline_affected)
        if baseline_affected is not None
        else baseline_affected_set(graph, config)
    )

    built: list[BuiltScenario] = []
    for family in chosen:
        spec = ScenarioSpec(
            family=family,
            magnitude=magnitude,
            shock_time=shock_time,
            seed=seed,
            # Each family gets the selection rule that makes it measurable.
            target_strategy=DEFAULT_STRATEGY_FOR_FAMILY.get(
                family, TargetStrategy.MOST_CONNECTED
            ),
        )
        try:
            built.append(
                build_scenario(
                    graph,
                    spec,
                    dataset_id=dataset_id,
                    baseline_affected=already_failing,
                )
            )
        except ValidationError:
            continue
    return built
