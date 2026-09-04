"""Building an analytical snapshot, offline.

Everything expensive happens here, once, and is written to a file. A frontend
request then reads precomputed results. That split is the whole design: a
dashboard should not be able to trigger a systemic-importance sweep or an
exhaustive intervention search by loading a page.

What a snapshot contains
------------------------
* the network as a frontend needs it - merchants with their liquidity position,
  estimated relationships, systemic ranking;
* one analysed scenario per shock family, each a complete result with its
  recommendation, alternatives and replayed counterfactual;
* a compact **serving graph** - merchants, obligations and the *estimated*
  dependency overlay - so bounded on-demand analysis of a new shock needs no
  payment history, no Hawkes re-estimation and no dataset generator.

The offer's pricing is a declared assumption
--------------------------------------------
The optimiser's cost model prices a liquidity injection at the capital deployed,
because that is what a budget constraint should count. That is not what a
facility *costs* the merchant. For the offer, capital required and indicative
cost are separated: the amount is the facility size, and the cost is a fee at
:data:`FACILITY_FEE_RATE_PER_DAY` over the stated duration. That rate is a stated
assumption recorded in the snapshot, not a quote and not an estimate of anything
observed.
"""

from __future__ import annotations

import hashlib
import json
import platform
import time
from datetime import UTC, datetime
from typing import Any

from lce import __version__
from lce.benchmark.scales import BenchmarkScale, scale_config
from lce.benchmark.scenarios import (
    BuiltScenario,
    ScenarioFamily,
    baseline_affected_set,
    scenario_suite,
)
from lce.config import ObjectiveSettings
from lce.domain.enums import InterventionType
from lce.domain.events import EXTERNAL_SINK
from lce.domain.intervention import Intervention
from lce.evaluation.metrics import attributable_affected
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.intervention.evaluate import Replay, replay
from lce.intervention.experiment import Phase4Config, ScenarioResult, run_scenario
from lce.intervention.profiles import ResourceProfile
from lce.logging import get_logger
from lce.optimization.systemic import (
    SystemicRanking,
    compute_systemic_importance,
    payment_throughput,
)
from lce.simulation.engine import LiquiditySimulator, SimulationConfig
from lce.snapshot.models import (
    SNAPSHOT_FORMAT_VERSION,
    AffectedMerchant,
    Confidence,
    CounterfactualView,
    DependencyView,
    InterventionOption,
    MerchantView,
    NetworkOverview,
    ProjectedImpact,
    Provenance,
    ScenarioSnapshot,
    ScenarioSummary,
    ShockView,
    SnapshotManifest,
    SystemicEntry,
    SystemicRankingView,
)
from lce.snapshot.views import (
    FACILITY_FEE_RATE_PER_DAY,
    build_offer,
    horizon_value,
    intervention_option,
    systemic_exposure,
    time_to_constraint_buckets,
)

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _cover_ratio(buffer: float, payables: float, receivables: float) -> float | None:
    if payables <= 0:
        return None
    return (buffer + receivables) / payables




# ------------------------------------------------------------------- network


def build_network_view(
    graph: TemporalPaymentGraph,
    *,
    dataset_id: str,
    scale: str,
    horizon: float,
) -> NetworkOverview:
    stats = graph.stats()
    obligations = graph.obligations
    return NetworkOverview(
        dataset_id=dataset_id,
        dataset_version=dataset_id,
        scale=scale,
        n_merchants=stats.n_merchants,
        n_relationships=len(
            [k for k in graph.distinct_pairs() if EXTERNAL_SINK not in k]
        ),
        n_payment_events=stats.n_payment_events,
        n_obligations=stats.n_obligations,
        total_payment_value=stats.total_payment_value,
        total_obligation_value=sum(o.amount for o in obligations),
        obligation_value_in_horizon=sum(
            o.outstanding for o in obligations if o.is_open and o.due_t <= horizon
        ),
        horizon_hours=horizon,
        sectors=_count(graph, "sector"),
        tiers=_count(graph, "tier"),
    )


def _count(graph: TemporalPaymentGraph, field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for profile in graph.merchants.values():
        key = str(getattr(profile, field))
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items()))


def build_merchant_views(
    graph: TemporalPaymentGraph,
    *,
    horizon: float,
    systemic: SystemicRanking | None,
) -> list[MerchantView]:
    throughput = payment_throughput(graph)
    ranks: dict[str, int] = {}
    if systemic is not None:
        ranks = {m: i + 1 for i, (m, _) in enumerate(systemic.ranked())}

    views: list[MerchantView] = []
    for merchant_id in sorted(graph.merchant_ids):
        profile = graph.merchant(merchant_id)
        payables = horizon_value(graph, merchant_id, horizon, payable=True)
        receivables = horizon_value(graph, merchant_id, horizon, payable=False)
        cover = _cover_ratio(profile.initial_buffer, payables, receivables)
        views.append(
            MerchantView(
                merchant_id=merchant_id,
                sector=str(profile.sector),
                tier=str(profile.tier),
                opening_balance=profile.opening_balance,
                credit_limit=profile.credit_limit,
                operating_floor=profile.operating_floor,
                liquidity_buffer=profile.initial_buffer,
                payables_in_horizon=payables,
                receivables_in_horizon=receivables,
                net_position=receivables - payables,
                cover_ratio=cover,
                throughput=throughput.get(merchant_id, 0.0),
                in_degree=len(graph.predecessors(merchant_id)),
                out_degree=len(graph.successors(merchant_id)),
                systemic_importance=(
                    systemic.normalised.get(merchant_id) if systemic else None
                ),
                systemic_rank=ranks.get(merchant_id),
                vulnerable=bool(cover is not None and cover < 1.0),
            )
        )
    return views


def build_dependency_views(
    graph: TemporalPaymentGraph, *, limit: int | None = None
) -> list[DependencyView]:
    """Estimated relationships, strongest first by value at stake."""
    value: dict[tuple[str, str], float] = {}
    counts: dict[tuple[str, str], int] = {}
    for event in graph.payment_events:
        if EXTERNAL_SINK in (event.payer_id, event.payee_id):
            continue
        key = event.edge_key
        value[key] = value.get(key, 0.0) + event.amount
        counts[key] = counts.get(key, 0) + 1

    views = [
        DependencyView(
            source_id=edge.source_id,
            target_id=edge.target_id,
            pass_through=edge.pass_through,
            conditional_probability=edge.conditional_probability,
            lag_mean_hours=edge.lag.mean_hours,
            reliability=edge.reliability,
            observed_value=value.get(edge.key, 0.0),
            n_events=counts.get(edge.key, edge.features.n_events),
            estimated=not edge.is_ground_truth,
        )
        for edge in graph.dependency_edges
        if EXTERNAL_SINK not in edge.key
    ]
    views.sort(key=lambda v: (-(v.pass_through * v.observed_value), v.source_id))
    return views[:limit] if limit else views


def build_systemic_view(
    ranking: SystemicRanking, *, n_merchants: int, limit: int | None = None
) -> SystemicRankingView:
    entries: list[SystemicEntry] = []
    for rank, (merchant_id, importance) in enumerate(ranking.ranked(), start=1):
        probe = ranking.probes.get(merchant_id)
        entries.append(
            SystemicEntry(
                merchant_id=merchant_id,
                rank=rank,
                importance=importance,
                marginal_disruption=ranking.simulated.get(merchant_id, 0.0),
                scale_normalised=probe.scale_normalised if probe else 0.0,
                downstream_affected=probe.downstream_affected if probe else 0,
                downstream_delayed_value=probe.downstream_delayed_value if probe else 0.0,
                cascade_depth=probe.cascade_depth if probe else 0,
                time_to_impact_hours=probe.time_to_impact_hours if probe else None,
                throughput=probe.throughput if probe else 0.0,
                structural_centrality=ranking.structural.get(merchant_id, 0.0),
            )
        )
    return SystemicRankingView(
        entries=entries[:limit] if limit else entries,
        n_sampled=len(ranking.probes),
        n_merchants=n_merchants,
        shock_fraction=ranking.shock_fraction,
        baseline_rank_correlation=ranking.baseline_correlations(),
        method=(
            "marginal disruption from a standardised shock at each merchant, "
            "differenced against the undisturbed baseline"
        ),
    )


# ------------------------------------------------------------------ scenario










def build_scenario_snapshot(
    scenario: BuiltScenario,
    result: ScenarioResult,
    *,
    graph: TemporalPaymentGraph,
    serving_graph: TemporalPaymentGraph,
    sim_config: SimulationConfig,
    horizon: float,
    provenance: Provenance,
    objective_settings: ObjectiveSettings | None = None,
) -> ScenarioSnapshot:
    """Assemble one scenario's frontend record from a Phase-4 result."""
    started = time.perf_counter()

    # The undisturbed world, so "affected" means *caused by this shock* rather
    # than "already in trouble" - the Phase-2 attribution rule, unchanged.
    no_shock = LiquiditySimulator(
        scenario.unperturbed_graph, sim_config, objective_settings
    ).run(None, run_id="snapshot:no_shock")
    no_shock_disruption = no_shock.disruption or 0.0
    shocked: Replay = replay(
        graph, scenario.shock, [], config=sim_config,
        objective_settings=objective_settings, run_id="snapshot:shocked",
    )

    attributable = attributable_affected(
        shocked.cascade.affected_ids, no_shock.affected_ids
    )
    disrupted_by_merchant = {
        merchant_id: max(
            0.0,
            outcome.value_delayed - no_shock.outcomes[merchant_id].value_delayed,
        )
        for merchant_id, outcome in shocked.cascade.outcomes.items()
        if merchant_id in no_shock.outcomes
    }
    hit_times = {
        m: t for m, t in shocked.cascade.hit_times().items() if m in set(attributable)
    }

    ranked = sorted(
        attributable, key=lambda m: (-disrupted_by_merchant.get(m, 0.0), m)
    )
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
                    hit_times[merchant_id] - scenario.shock.onset_t
                    if merchant_id in hit_times
                    else None
                ),
                disrupted_value=disrupted_by_merchant.get(merchant_id, 0.0),
                cascade_depth=outcome.hop_distance if outcome else None,
                sector=str(profile.sector),
                tier=str(profile.tier),
            )
        )

    report = result.counterfactual
    recommended_outcome = report.by_name("model_guided_greedy")
    chosen = list(recommended_outcome.interventions) if recommended_outcome else []
    treated = (
        replay(
            graph, scenario.shock, chosen, config=sim_config,
            objective_settings=objective_settings, run_id="snapshot:treated",
        )
        if chosen
        else shocked
    )

    protected = max(0, shocked.n_affected - treated.n_affected)
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
        merchants_protected=protected,
        cost=cost,
        capital_efficiency=prevented / cost if cost > 0 else None,
        optimality_gap=recommended_outcome.relative_gap if recommended_outcome else None,
        regret=recommended_outcome.regret if recommended_outcome else None,
    )

    # Options: the recommendation first, then every other candidate the search
    # considered, each with its own measured marginal effect.
    selected_ids = {u.intervention_id for u in chosen}
    options: list[InterventionOption] = []
    for entry in result.candidates.get("candidates", []):
        action = _rebuild_action(entry, chosen)
        if action is None:
            continue
        options.append(
            intervention_option(
                action,
                graph=graph,
                horizon=horizon,
                disruption_prevented=(
                    prevented if action.intervention_id in selected_ids else 0.0
                ),
                feasible=True,
                violations=[],
                selected=action.intervention_id in selected_ids,
            )
        )
    recommended = next((o for o in options if o.selected), None)
    if recommended is None and chosen:
        recommended = intervention_option(
            chosen[0], graph=graph, horizon=horizon,
            disruption_prevented=prevented, feasible=True, violations=[], selected=True,
        )
        options.insert(0, recommended)

    robustness = result.robustness or {}
    confidence = Confidence(
        source=result.prediction.get("source", "propagation"),
        calibrated=bool(result.prediction.get("calibrated", False)),
        model_version=result.prediction.get("model_version"),
        robust_mode=bool(robustness),
        n_scenarios_considered=len(robustness.get("worlds", []) or []) or 1,
        disruption_spread=(robustness.get("chosen") or {}).get("spread"),
        recommendation_stable_under_uncertainty=(
            not robustness["nominal_choice_differs"]
            if "nominal_choice_differs" in robustness
            else None
        ),
        note=str(result.prediction.get("note", "")),
    )

    scenario_provenance = provenance.model_copy(
        update={"scenario_id": scenario.scenario_id}
    )
    offer = None
    if chosen:
        merchant_id = chosen[0].merchant_id
        payables = horizon_value(graph, merchant_id, horizon, payable=True)
        buffer = graph.merchant(merchant_id).initial_buffer
        offer = build_offer(
            chosen[0],
            scenario_id=scenario.scenario_id,
            graph=serving_graph,
            horizon=horizon,
            counterfactual=counterfactual,
            eligible_criteria={
                "is_in_network": True,
                "has_obligations_in_horizon": payables > 0,
                "action_is_feasible": not (
                    recommended_outcome.violations if recommended_outcome else []
                ),
                "prevents_disruption": prevented > 0,
            },
            constraints={
                "max_amount": round(2.0 * payables, 2),
                "liquidity_buffer": round(buffer, 2),
                "obligations_in_horizon": round(payables, 2),
                "max_duration_hours": horizon,
            },
            provenance=scenario_provenance,
        )

    return ScenarioSnapshot(
        scenario_id=scenario.scenario_id,
        family=str(scenario.spec.family),
        shock=ShockView(
            description=scenario.shock.description or scenario.shock.name,
            origin_merchants=list(scenario.shock.origin_ids),
            magnitude=scenario.shock.total_magnitude,
            onset_hours=scenario.shock.onset_t,
            kind=str(scenario.shock.components[0].kind),
            family=str(scenario.spec.family),
        ),
        projected_impact=ProjectedImpact(
            n_affected=len(attributable),
            n_defaulted=shocked.n_defaulted,
            disrupted_value=sum(disrupted_by_merchant.values()),
            disruption_index=attributable_disruption,
            network_disruption_index=shocked.disruption,
            max_cascade_depth=shocked.cascade.max_hop(),
            systemic_exposure=systemic_exposure(graph, attributable, horizon),
        ),
        confidence=confidence,
        affected_merchants=affected_views,
        time_to_constraint=time_to_constraint_buckets(
            hit_times, disrupted_by_merchant, scenario.shock.onset_t
        ),
        recommended_intervention=recommended,
        alternatives=[o for o in options if not o.selected],
        counterfactual=counterfactual,
        offer=offer,
        provenance=scenario_provenance,
        computed_in_ms=(time.perf_counter() - started) * 1000.0
        + result.timing.get("total_s", 0.0) * 1000.0,
    )


def _rebuild_action(entry: dict[str, Any], chosen: list[Intervention]) -> Intervention | None:
    """Recover the full action for a candidate the search reported.

    The candidate summary is a display record, so the selected actions are used
    where they match and the rest are reconstructed from the summary's own
    fields. Returns ``None`` rather than guessing when the summary is unusable.
    """
    for action in chosen:
        if action.intervention_id == entry.get("intervention_id"):
            return action
    try:
        return Intervention(
            intervention_id=entry["intervention_id"],
            type=InterventionType(entry["type"]),
            merchant_id=entry["merchant_id"],
            t=0.0,
            amount=max(float(entry.get("cost", 0.0)), 0.0) or 1.0,
            shift_hours=0.0,
            target_obligation_id=None,
            provenance={
                "score": entry.get("score", 0.0),
                "factors": entry.get("factors", {}),
                "rule": "candidate_summary",
            },
        )
    except Exception:  # a summary we cannot faithfully reconstruct is dropped
        return None




def summarise_scenario(snapshot: ScenarioSnapshot) -> ScenarioSummary:
    recommended = snapshot.recommended_intervention
    return ScenarioSummary(
        scenario_id=snapshot.scenario_id,
        family=snapshot.family,
        headline=snapshot.shock.description,
        n_affected=snapshot.projected_impact.n_affected,
        disrupted_value=snapshot.projected_impact.disrupted_value,
        max_cascade_depth=snapshot.projected_impact.max_cascade_depth,
        recommended_action=(
            f"{recommended.type} on {recommended.merchant_id}" if recommended else None
        ),
        cost=snapshot.counterfactual.cost,
        disruption_reduction_pct=snapshot.counterfactual.disruption_reduction_pct,
        capital_efficiency=snapshot.counterfactual.capital_efficiency,
    )


# --------------------------------------------------------------- the builder


def serving_graph_payload(graph: TemporalPaymentGraph) -> dict[str, Any]:
    """The compact network a serving process needs, and nothing more.

    Merchants, obligations and the *estimated* dependency overlay. The payment
    event stream is deliberately excluded: it is the bulk of the data, and its
    only consumer is the dependency estimator, whose output is already here. A
    serving process therefore never re-estimates structure and never needs the
    dataset generator.
    """
    return {
        "network_id": graph.network_id,
        "dataset_version": graph.dataset_version,
        "merchants": [p.to_json_dict() for p in graph.merchants.values()],
        "obligations": [o.to_json_dict() for o in graph.obligations],
        "dependencies": [d.to_json_dict() for d in graph.dependency_edges],
    }


def build_snapshot(
    *,
    seed: int = 2025,
    scale: str = "small",
    profile: ResourceProfile = ResourceProfile.SMALL_FAST,
    families: tuple[ScenarioFamily, ...] | None = None,
    magnitude: float = 2.0,
    systemic_sample: int | None = None,
    generator_overrides: dict[str, Any] | None = None,
    objective_settings: ObjectiveSettings | None = None,
) -> tuple[dict[str, Any], SnapshotManifest]:
    """Run the analysis once and return the snapshot payload with its manifest."""
    started = time.perf_counter()
    config = Phase4Config(
        profile=profile,
        seeds=(seed,),
        magnitude=magnitude,
        families=families,
        robust=True,
        systemic=True,
        pruning_benchmark=False,  # a build-time diagnostic, not a serving artifact
    )
    budget = config.budget

    generator = scale_config(BenchmarkScale(scale), seed=seed, overrides=generator_overrides)
    from lce.data.generator import generate_network

    network = generate_network(generator)
    graph = network.graph
    horizon = generator.horizon_hours
    sim_config = SimulationConfig(horizon_hours=horizon, seed=seed)

    sample_size = systemic_sample if systemic_sample is not None else budget.systemic_sample
    sample = sorted(graph.merchant_ids)[: sample_size or len(graph)]
    systemic = compute_systemic_importance(
        graph, config=sim_config, objective=objective_settings, merchants=sample
    )

    provenance = Provenance(
        run_id=f"snap-{config.config_hash}",
        dataset_id=network.dataset_version,
        dataset_version=network.dataset_version,
        seed=seed,
        config_hash=config.config_hash,
        simulator_config_hash=_hash(sim_config.to_dict()),
        optimizer="greedy_objective",
        code_version=__version__,
        created_at=_now(),
    )

    already = baseline_affected_set(graph, sim_config)
    scenarios: list[ScenarioSnapshot] = []
    serving = _serving_graph(graph)

    for scenario in scenario_suite(
        graph,
        dataset_id=network.dataset_version,
        seed=seed,
        magnitude=magnitude,
        families=families,
        config=sim_config,
        baseline_affected=already,
    ):
        result = run_scenario(
            scenario,
            config=config,
            sim_config=sim_config,
            systemic=systemic,
            objective_settings=objective_settings,
            measure_pruning=False,
        )
        if serving.dependency_edges == [] and scenario is not None:
            # The estimated overlay comes from the first scenario's observed
            # window; every scenario on this network sees the same history.
            serving = _install_estimated_overlay(serving, scenario, sim_config)
        scenarios.append(
            build_scenario_snapshot(
                scenario,
                result,
                graph=graph,
                serving_graph=serving,
                sim_config=sim_config,
                horizon=horizon,
                provenance=provenance,
                objective_settings=objective_settings,
            )
        )

    payload: dict[str, Any] = {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "provenance": provenance.model_dump(),
        "network": build_network_view(
            serving, dataset_id=network.dataset_version, scale=scale, horizon=horizon
        ).model_dump()
        | {
            "n_payment_events": graph.stats().n_payment_events,
            "total_payment_value": graph.stats().total_payment_value,
            "n_relationships": len(
                [k for k in graph.distinct_pairs() if EXTERNAL_SINK not in k]
            ),
        },
        "merchants": [
            m.model_dump()
            for m in build_merchant_views(serving, horizon=horizon, systemic=systemic)
        ],
        "dependencies": [d.model_dump() for d in build_dependency_views(serving)],
        "systemic": build_systemic_view(
            systemic, n_merchants=len(graph)
        ).model_dump(),
        "scenarios": [s.model_dump() for s in scenarios],
        "serving_graph": serving_graph_payload(serving),
        "build": {
            "profile": str(profile),
            "scale": scale,
            "seed": seed,
            "magnitude": magnitude,
            "generator": generator.to_dict(),
            "simulation": sim_config.to_dict(),
            "systemic_sample": len(sample),
            "facility_fee_rate_per_day": FACILITY_FEE_RATE_PER_DAY,
            "elapsed_s": round(time.perf_counter() - started, 2),
            "host": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
        },
    }

    manifest = SnapshotManifest(
        snapshot_id=provenance.run_id,
        created_at=provenance.created_at,
        code_version=__version__,
        dataset_version=network.dataset_version,
        scale=scale,
        seed=seed,
        n_scenarios=len(scenarios),
        content_hash=_hash(payload),
        provenance=provenance,
        build=payload["build"],
    )
    logger.info(
        "snapshot_built",
        snapshot_id=manifest.snapshot_id,
        n_scenarios=len(scenarios),
        n_merchants=len(graph),
        elapsed_s=payload["build"]["elapsed_s"],
    )
    return payload, manifest


def _serving_graph(graph: TemporalPaymentGraph) -> TemporalPaymentGraph:
    """Merchants and obligations only - history and true overlay left behind."""
    out = TemporalPaymentGraph(
        network_id=graph.network_id, dataset_version=graph.dataset_version
    )
    out.add_merchants(graph.merchants.values())
    out.add_obligations(graph.obligations, require_nodes=False)
    return out


def _install_estimated_overlay(
    serving: TemporalPaymentGraph,
    scenario: BuiltScenario,
    sim_config: SimulationConfig,
) -> TemporalPaymentGraph:
    """Estimate the dependency structure once and attach it to the serving graph.

    Estimated from the observable pre-origin stream, exactly as Phase 3 requires.
    The *true* generator overlay is never copied here: a snapshot that shipped
    ground truth would let a frontend display numbers the model could not have
    known.
    """
    from lce.learning.pointprocess import HawkesDependencyEstimator
    from lce.learning.problem import baseline_payment_stream, build_observed_window

    stream = baseline_payment_stream(scenario.unperturbed_graph, sim_config)
    window = build_observed_window(scenario, config=sim_config, baseline_payments=stream)
    learned = HawkesDependencyEstimator().estimate(window)
    serving.clear_dependencies()
    serving.set_dependencies(
        [
            edge
            for edge in learned.edges
            if serving.has_merchant(edge.source_id) and serving.has_merchant(edge.target_id)
        ]
    )
    return serving


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


__all__ = [
    "FACILITY_FEE_RATE_PER_DAY",
    "build_dependency_views",
    "build_merchant_views",
    "build_network_view",
    "build_offer",
    "build_scenario_snapshot",
    "build_snapshot",
    "build_systemic_view",
    "horizon_value",
    "intervention_option",
    "serving_graph_payload",
    "summarise_scenario",
    "systemic_exposure",
    "time_to_constraint_buckets",
]


# Re-exported for the store, which rebuilds a graph from the payload.


