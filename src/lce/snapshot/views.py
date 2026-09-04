"""Snapshot helpers shared by the offline build and the serving path.

Everything here is pure: it reads a payload or a graph and returns a view model.
That is the whole reason the module exists separately from
:mod:`lce.snapshot.build` - ``build`` imports the dataset generator, the
benchmark package and the Phase-4 experiment runner, and none of those may be
reachable from a process that only answers frontend requests. Serving imports
this module; ``build`` imports it too, so the two paths cannot drift.

``src/lce/scripts/audit_backend.py`` asserts the separation by importing the
serving modules in a clean interpreter and failing if a training or generation
module turns up in ``sys.modules``.
"""

from __future__ import annotations

import itertools
from typing import Any

from lce.domain.enums import InterventionType
from lce.domain.intervention import Intervention
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.intervention.actions import downstream_obligation_value
from lce.simulation.engine import SimulationConfig
from lce.snapshot.models import (
    CounterfactualView,
    InterventionOption,
    OfferContract,
    OfferEligibility,
    Provenance,
    RepaymentTerms,
    TimeToConstraintBucket,
)

HOURS_PER_DAY = 24.0

#: Declared assumption: the fee a temporary liquidity facility would carry, per
#: day on the principal. Same order as the supplier-carry rate already used by
#: the optimiser's cost model. Not a quote, not calibrated to any market.
FACILITY_FEE_RATE_PER_DAY = 0.0004

#: Time-to-constraint histogram edges, in hours from the shock.
TIME_BUCKETS: tuple[float, ...] = (0.0, 6.0, 12.0, 24.0, 48.0, 72.0, 120.0, 168.0)

#: Intervention types a merchant-facing offer can be built for. The term-only
#: actions change a counterparty agreement rather than extending anything to the
#: merchant, so they are recommendations without an offer attached.
OFFERABLE = frozenset(
    {
        InterventionType.LIQUIDITY_INJECTION,
        InterventionType.CREDIT_LINE_INCREASE,
        InterventionType.RECEIVABLE_ACCELERATION,
        InterventionType.REPAYMENT_RESTRUCTURE,
    }
)


def horizon_value(
    graph: TemporalPaymentGraph, merchant_id: str, horizon: float, *, payable: bool
) -> float:
    items = (
        graph.payables_of(merchant_id) if payable else graph.receivables_of(merchant_id)
    )
    return sum(o.outstanding for o in items if o.is_open and o.due_t <= horizon)


def _duration_hours(action: Intervention, horizon: float) -> float:
    match action.type:
        case InterventionType.SUPPLIER_TERM_EXTENSION | InterventionType.RECEIVABLE_ACCELERATION:
            return action.shift_hours
        case InterventionType.REPAYMENT_RESTRUCTURE:
            return max(action.tranches - 1, 0) * action.tranche_spacing_hours
        case _:
            # A facility runs to the end of the analysis horizon.
            return max(horizon - action.t, 0.0)


def intervention_option(
    action: Intervention,
    *,
    graph: TemporalPaymentGraph,
    horizon: float,
    disruption_prevented: float,
    feasible: bool,
    violations: list[str],
    selected: bool,
) -> InterventionOption:
    capital = (
        action.amount
        if action.type
        in (InterventionType.LIQUIDITY_INJECTION, InterventionType.CREDIT_LINE_INCREASE)
        else 0.0
    )
    provenance = action.provenance or {}
    factors = {
        k: float(v)
        for k, v in (provenance.get("factors") or {}).items()
        if isinstance(v, (int, float))
    }
    return InterventionOption(
        intervention_id=action.intervention_id,
        type=str(action.type),
        merchant_id=action.merchant_id,
        liquidity_required=capital,
        amount=action.amount,
        duration_hours=_duration_hours(action, horizon),
        apply_at_hours=action.t,
        cost=action.cost,
        predicted_downstream_disruption=downstream_obligation_value(
            graph, action.merchant_id, horizon
        ),
        disruption_prevented=disruption_prevented,
        capital_efficiency=(
            disruption_prevented / action.cost if action.cost > 0 else None
        ),
        confidence=float(min(max(provenance.get("score", 0.0), 0.0), 1.0)),
        feasible=feasible,
        constraint_violations=violations,
        rationale=factors,
        selected=selected,
    )


def time_to_constraint_buckets(
    hit_times: dict[str, float],
    disrupted: dict[str, float],
    onset: float,
) -> list[TimeToConstraintBucket]:
    total = max(len(hit_times), 1)
    seen = 0
    out: list[TimeToConstraintBucket] = []
    for low, high in itertools.pairwise(TIME_BUCKETS):
        members = [
            m for m, t in hit_times.items() if low <= max(t - onset, 0.0) < high
        ]
        seen += len(members)
        out.append(
            TimeToConstraintBucket(
                from_hours=low,
                to_hours=high,
                n_merchants=len(members),
                disrupted_value=sum(disrupted.get(m, 0.0) for m in members),
                cumulative_share=seen / total,
            )
        )
    return out


def build_offer(
    action: Intervention,
    *,
    scenario_id: str,
    graph: TemporalPaymentGraph,
    horizon: float,
    counterfactual: CounterfactualView,
    eligible_criteria: dict[str, bool],
    constraints: dict[str, float | int | None],
    provenance: Provenance,
) -> OfferContract | None:
    """Turn a recommended action into a structured, clearly-labelled proposal."""
    if action.type not in OFFERABLE:
        return None

    duration = max(_duration_hours(action, horizon), 1.0)
    days = duration / HOURS_PER_DAY
    fee = action.amount * FACILITY_FEE_RATE_PER_DAY * days
    annual = FACILITY_FEE_RATE_PER_DAY * 365.0 * 100.0

    if action.type is InterventionType.REPAYMENT_RESTRUCTURE:
        terms = RepaymentTerms(
            structure="instalments",
            n_instalments=action.tranches,
            instalment_amount=action.amount / max(action.tranches, 1),
            first_due_hours=action.t,
            cadence_hours=action.tranche_spacing_hours,
        )
    else:
        terms = RepaymentTerms(
            structure="bullet",
            n_instalments=1,
            instalment_amount=action.amount,
            first_due_hours=action.t + duration,
            cadence_hours=duration,
        )

    profile = graph.merchant(action.merchant_id)
    return OfferContract(
        offer_id=f"off_{action.intervention_id.removeprefix('itv_')}",
        merchant_id=action.merchant_id,
        scenario_id=scenario_id,
        intervention_type=str(action.type),
        proposed_amount=action.amount,
        duration_hours=duration,
        repayment=terms,
        indicative_cost=fee,
        indicative_rate_annual_pct=annual,
        rationale=(
            f"{action.merchant_id} holds "
            f"INR {horizon_value(graph, action.merchant_id, horizon, payable=True):,.0f} "
            f"of obligations due inside the horizon against a liquidity buffer of "
            f"INR {profile.initial_buffer:,.0f}. Replaying this action in the "
            f"simulator reduced network disruption by "
            f"{counterfactual.disruption_reduction_pct:.1f}% and preserved "
            f"INR {counterfactual.commerce_preserved:,.0f} of payment value that "
            f"would otherwise have been delayed."
        ),
        expected_network_benefit={
            "disruption_prevented": counterfactual.disruption_prevented,
            "commerce_preserved": counterfactual.commerce_preserved,
            "merchants_protected": float(counterfactual.merchants_protected),
            "disruption_reduction_pct": counterfactual.disruption_reduction_pct,
            "capital_efficiency": counterfactual.capital_efficiency or 0.0,
        },
        eligibility=OfferEligibility(
            eligible=all(eligible_criteria.values()),
            criteria=eligible_criteria,
            constraints=constraints,
        ),
        provenance=provenance,
    )


def systemic_exposure(
    graph: TemporalPaymentGraph, affected: list[str], horizon: float
) -> float:
    total = sum(
        o.outstanding for o in graph.obligations if o.is_open and o.due_t <= horizon
    )
    if total <= 0:
        return 0.0
    held = sum(horizon_value(graph, m, horizon, payable=True) for m in affected)
    return min(held / total, 1.0)


def graph_from_payload(payload: dict[str, Any]) -> TemporalPaymentGraph:
    """Rebuild the serving graph. No payment events, so no generator needed."""
    from lce.domain.edges import DependencyEdge
    from lce.domain.events import Obligation
    from lce.domain.merchant import MerchantProfile

    graph = TemporalPaymentGraph(
        network_id=payload.get("network_id", "serving"),
        dataset_version=payload.get("dataset_version"),
    )
    graph.add_merchants(MerchantProfile.model_validate(m) for m in payload["merchants"])
    graph.add_obligations(
        (Obligation.model_validate(o) for o in payload["obligations"]), require_nodes=False
    )
    graph.set_dependencies(
        DependencyEdge.model_validate(d) for d in payload.get("dependencies", [])
    )
    return graph


def scenario_config(payload: dict[str, Any]) -> tuple[SimulationConfig, float]:
    """Recover the simulator configuration a snapshot was built with."""
    build = payload["build"]
    sim = build["simulation"]
    config = SimulationConfig(
        horizon_hours=float(sim["horizon_hours"]),
        tick_hours=float(sim["tick_hours"]),
        grace_period_hours=float(sim["grace_period_hours"]),
        partial_payment_enabled=bool(sim["partial_payment_enabled"]),
        min_partial_fraction=float(sim["min_partial_fraction"]),
        seed=int(sim["seed"]),
    )
    return config, float(sim["horizon_hours"])
