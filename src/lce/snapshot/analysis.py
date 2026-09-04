"""On-demand scenario analysis, bounded by construction.

The path a request takes when a caller asks "what if *this* merchant misses a
payment?". It reuses the Phase-4 machinery - candidate generation, feasibility,
greedy search over the true simulator, replay - and reuses the snapshot's
*estimated* dependency overlay rather than re-deriving it.

Why this is cheap
-----------------
The expensive parts of Phase 4 are structure estimation (marked-Hawkes EM per
link) and exhaustive search. Neither runs here. The overlay is already in the
snapshot, and the search is greedy over at most twelve candidates with at most
two actions, so the cost is bounded at roughly ``2 x 12`` simulations of a
network the store has already refused to load if it is too large.

What it does not do
-------------------
No exhaustive optimum, so no optimality gap is reported - a gap against a
heuristic would be a number pretending to be a bound. No robustness sweep by
default. Both are build-time analyses, and the precomputed scenarios carry them.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from lce.domain.enums import ShockKind
from lce.domain.shock import Shock, ShockComponent
from lce.intervention.actions import generate_actions
from lce.intervention.evaluate import replay
from lce.intervention.problem import InterventionConstraints, ObjectiveSpec, check_action
from lce.intervention.scalable import greedy_solve
from lce.logging import get_logger
from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
from lce.seeds import config_hash
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.snapshot.models import (
    AffectedMerchant,
    Confidence,
    CounterfactualView,
    InterventionOption,
    ProjectedImpact,
    ScenarioSnapshot,
    ShockView,
)
from lce.snapshot.views import (
    build_offer,
    horizon_value,
    intervention_option,
    systemic_exposure,
    time_to_constraint_buckets,
)

if TYPE_CHECKING:  # pragma: no cover
    from lce.snapshot.store import SnapshotStore

logger = get_logger(__name__)


def _liquidity_slack(store: SnapshotStore, merchant_id: str) -> float:
    """The merchant's cushion: buffer plus what it is owed, less what it owes.

    Used to size a shock relative to the merchant rather than in absolute
    rupees, so the same multiple means the same severity on a micro merchant and
    on an anchor. Mirrors the Phase-2 scenario sizing rule.
    """
    profile = store.graph.merchant(merchant_id)
    horizon = store.horizon_hours
    receivables = horizon_value(store.graph, merchant_id, horizon, payable=False)
    payables = horizon_value(store.graph, merchant_id, horizon, payable=True)
    return max(
        profile.initial_buffer + receivables - payables,
        profile.initial_buffer * 0.25,
        1.0,
    )


def analyse_shock(
    store: SnapshotStore,
    *,
    merchant_ids: list[str],
    magnitude_multiple: float,
    onset_hours: float,
    max_actions: int,
) -> ScenarioSnapshot:
    """Build, analyse and replay one caller-specified shock."""
    started = time.perf_counter()
    graph = store.graph
    sim_config = store.sim_config
    horizon = store.horizon_hours

    components = [
        ShockComponent(
            merchant_id=merchant_id,
            magnitude=max(_liquidity_slack(store, merchant_id) * magnitude_multiple, 1.0),
            t=onset_hours,
            kind=ShockKind.CASH_WITHDRAWAL,
        )
        for merchant_id in sorted(set(merchant_ids))
    ]
    shock = Shock(
        name="on_demand",
        description=(
            f"{', '.join(c.merchant_id for c in components)} "
            f"{'lose' if len(components) > 1 else 'loses'} "
            f"INR {sum(c.magnitude for c in components):,.0f} of liquidity at "
            f"t={onset_hours:.0f}h"
        ),
        components=components,
    )
    scenario_id = "adhoc-" + config_hash(
        {
            "snapshot": store.snapshot_id,
            "merchants": sorted(set(merchant_ids)),
            "magnitude": magnitude_multiple,
            "onset": onset_hours,
            "max_actions": max_actions,
        },
        length=12,
    )

    # The no-shock world, so "affected" means caused by this shock.
    from lce.simulation.engine import LiquiditySimulator

    no_shock = LiquiditySimulator(graph, sim_config).run(None, run_id="adhoc:no_shock")
    no_shock_disruption = no_shock.disruption or 0.0
    shocked = replay(graph, shock, [], config=sim_config, run_id="adhoc:shocked")

    from lce.evaluation.metrics import attributable_affected

    attributable = attributable_affected(
        shocked.cascade.affected_ids, no_shock.affected_ids
    )
    disrupted = {
        merchant_id: max(
            0.0, outcome.value_delayed - no_shock.outcomes[merchant_id].value_delayed
        )
        for merchant_id, outcome in shocked.cascade.outcomes.items()
        if merchant_id in no_shock.outcomes
    }
    hit_times = {
        m: t for m, t in shocked.cascade.hit_times().items() if m in set(attributable)
    }

    # --- decide, over the true simulator, inside the bound -------------------
    constraints = InterventionConstraints(
        max_actions=max_actions, horizon_hours=horizon, decision_time=onset_hours
    )
    prediction = LinearThresholdPropagator(
        PropagationConfig(horizon_hours=horizon)
    ).predict(graph, shock)
    action_set = generate_actions(
        graph, shock, prediction, constraints=constraints, max_candidates=12
    )
    solved = greedy_solve(
        CounterfactualEvaluator(graph=graph, shock=shock, config=sim_config),
        action_set.interventions,
        graph,
        constraints=constraints,
        objective=ObjectiveSpec(),
    )
    chosen = list(solved.interventions)
    treated = (
        replay(graph, shock, chosen, config=sim_config, run_id="adhoc:treated")
        if chosen
        else shocked
    )

    prevented = shocked.disruption - treated.disruption
    cost = sum(u.cost for u in chosen)
    attributable_disruption = max(0.0, shocked.disruption - no_shock_disruption)
    counterfactual = CounterfactualView(
        baseline_disruption=shocked.disruption,
        attributable_disruption=attributable_disruption,
        baseline_disrupted_value=shocked.value_delayed,
        baseline_affected=shocked.n_affected,
        with_intervention_disruption=treated.disruption,
        with_intervention_disrupted_value=treated.value_delayed,
        with_intervention_affected=treated.n_affected,
        disruption_prevented=prevented,
        disruption_reduction_pct=(
            100.0 * prevented / shocked.disruption if shocked.disruption > 0 else 0.0
        ),
        commerce_preserved=shocked.value_delayed - treated.value_delayed,
        merchants_protected=max(0, shocked.n_affected - treated.n_affected),
        cost=cost,
        capital_efficiency=prevented / cost if cost > 0 else None,
        # No exhaustive optimum is computed on this path, so no gap is claimed.
        optimality_gap=None,
        regret=None,
    )

    selected = {u.intervention_id for u in chosen}
    options: list[InterventionOption] = []
    for entry in action_set.scored:
        action = entry.intervention
        report = check_action([action], graph, constraints)
        options.append(
            intervention_option(
                action,
                graph=graph,
                horizon=horizon,
                disruption_prevented=(
                    prevented if action.intervention_id in selected else 0.0
                ),
                feasible=report.feasible,
                violations=report.names(),
                selected=action.intervention_id in selected,
            )
        )
    recommended = next((o for o in options if o.selected), None)

    ranked = sorted(attributable, key=lambda m: (-disrupted.get(m, 0.0), m))
    affected_views = []
    for rank, merchant_id in enumerate(ranked, start=1):
        profile = graph.merchant(merchant_id)
        outcome = shocked.cascade.outcomes.get(merchant_id)
        affected_views.append(
            AffectedMerchant(
                merchant_id=merchant_id,
                rank=rank,
                probability_constrained=None,
                time_to_constraint_hours=(
                    hit_times[merchant_id] - onset_hours
                    if merchant_id in hit_times
                    else None
                ),
                disrupted_value=disrupted.get(merchant_id, 0.0),
                cascade_depth=outcome.hop_distance if outcome else None,
                sector=str(profile.sector),
                tier=str(profile.tier),
            )
        )

    provenance = store.provenance.model_copy(update={"scenario_id": scenario_id})
    offer = None
    if chosen:
        merchant_id = chosen[0].merchant_id
        payables = horizon_value(graph, merchant_id, horizon, payable=True)
        offer = build_offer(
            chosen[0],
            scenario_id=scenario_id,
            graph=graph,
            horizon=horizon,
            counterfactual=counterfactual,
            eligible_criteria={
                "is_in_network": True,
                "has_obligations_in_horizon": payables > 0,
                "action_is_feasible": solved.feasible,
                "prevents_disruption": prevented > 0,
            },
            constraints={
                "max_amount": round(2.0 * payables, 2),
                "liquidity_buffer": round(graph.merchant(merchant_id).initial_buffer, 2),
                "obligations_in_horizon": round(payables, 2),
                "max_duration_hours": horizon,
            },
            provenance=provenance,
        )

    elapsed = (time.perf_counter() - started) * 1000.0
    logger.info(
        "on_demand_scenario_analysed",
        scenario_id=scenario_id,
        n_origins=len(components),
        n_affected=len(attributable),
        n_candidates=len(action_set),
        simulations=solved.simulations,
        elapsed_ms=round(elapsed, 1),
    )

    return ScenarioSnapshot(
        scenario_id=scenario_id,
        family="on_demand",
        shock=ShockView(
            description=shock.description,
            origin_merchants=list(shock.origin_ids),
            magnitude=shock.total_magnitude,
            onset_hours=onset_hours,
            kind=str(ShockKind.CASH_WITHDRAWAL),
            family="on_demand",
        ),
        projected_impact=ProjectedImpact(
            n_affected=len(attributable),
            n_defaulted=shocked.n_defaulted,
            disrupted_value=sum(disrupted.values()),
            disruption_index=attributable_disruption,
            network_disruption_index=shocked.disruption,
            max_cascade_depth=shocked.cascade.max_hop(),
            systemic_exposure=systemic_exposure(graph, attributable, horizon),
        ),
        confidence=Confidence(
            source="propagation",
            calibrated=False,
            model_version=None,
            robust_mode=False,
            n_scenarios_considered=1,
            disruption_spread=None,
            recommendation_stable_under_uncertainty=None,
            note=(
                "on-demand analysis: candidates ranked by the analytic propagator "
                "over the snapshot's estimated dependency overlay, then chosen by "
                "greedy search over the simulator. No exhaustive optimum and no "
                "robustness sweep are computed on this path, so no optimality gap "
                "is reported."
            ),
        ),
        affected_merchants=affected_views,
        time_to_constraint=time_to_constraint_buckets(hit_times, disrupted, onset_hours),
        recommended_intervention=recommended,
        alternatives=[o for o in options if not o.selected],
        counterfactual=counterfactual,
        offer=offer,
        provenance=provenance,
        computed_in_ms=elapsed,
    )


def analysis_metadata(store: SnapshotStore) -> dict[str, Any]:
    """What the on-demand path will and will not do, for a client to display."""
    return {
        "bounded": True,
        "limits": store.analysis_limits(),
        "computes": [
            "attributable affected set",
            "time-to-constraint distribution",
            "bounded greedy intervention search over the true simulator",
            "replayed counterfactual",
        ],
        "does_not_compute": [
            "exhaustive optimum and optimality gap",
            "robustness sweep across perturbed worlds",
            "dependency re-estimation",
        ],
    }
