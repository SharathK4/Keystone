"""The admin data contract, computed from the snapshot.

Every number here is derived from the loaded snapshot or probed from the payment
provider at call time. Nothing is a constant, a placeholder, or a plausible
figure typed into a template - if a value cannot be computed it is ``None`` and
the client decides how to render an absence.

Two figures are worth reading carefully:

``total_value_exposed``
    obligation value held by merchants whose cover ratio is below one, inside the
    horizon. It is the money sitting with merchants that cannot clear their own
    book unaided - not a loss estimate, and not a prediction.

``mean_failure_probability``
    the mean, across precomputed scenarios, of the share of the scored network
    that the shock actually reached. It is a *frequency observed in simulation*,
    not a calibrated probability of anything happening in the world, and the
    field is named to keep that distinction visible in the payload.
"""

from __future__ import annotations

from typing import Any

from lce.logging import get_logger
from lce.snapshot.models import (
    DashboardSummary,
    ExecutionStatus,
    OfferContract,
    ScenarioSummary,
)
from lce.snapshot.store import SnapshotStore

logger = get_logger(__name__)


def execution_status(*, probe: bool = True) -> ExecutionStatus:
    """What the payment provider can do right now, probed rather than assumed.

    Falls back to the simulation provider's answer whenever Razorpay is
    unconfigured, unreachable, or lacks the product an action would need. The
    optimiser never depends on any of this: execution is a separate concern from
    recommendation, and a provider being down changes what can be *carried out*,
    not what is *advised*.
    """
    from lce.execution.providers import _REQUIRED_CAPABILITY, RazorpayTestProvider

    capabilities: dict[str, bool] = {}
    configured = False
    mode = "test"
    note = ""
    try:
        provider = RazorpayTestProvider()
        configured = provider.config.configured
        mode = str(provider.config.mode)
        capabilities = provider.capabilities() if probe else {}
    except Exception as exc:  # unconfigured or refused: report, never crash
        note = f"provider unavailable: {type(exc).__name__}"

    executable = [
        str(kind)
        for kind, needed in _REQUIRED_CAPABILITY.items()
        if capabilities.get(needed, False)
    ]
    if not note:
        note = (
            "All recommendations are produced and evaluated in simulation. "
            "Razorpay Test Mode is used for connectivity and capability probing; "
            "no funds move. "
            + (
                f"{len(executable)} intervention type(s) map to an available "
                "Test-Mode operation."
                if executable
                else "No intervention type maps to an available Test-Mode "
                "operation on this account, so every action is recorded as a plan."
            )
        )

    return ExecutionStatus(
        provider="razorpay_test",
        mode=mode,
        configured=configured,
        api_reachable=bool(capabilities.get("api_reachable", False)),
        capabilities=capabilities,
        executable_intervention_types=sorted(executable),
        fallback_provider="simulation",
        note=note,
    )


def _best_offer(store: SnapshotStore) -> OfferContract | None:
    offers = store.offers()
    if not offers:
        return None
    return max(
        offers,
        key=lambda o: o.expected_network_benefit.get("disruption_prevented", 0.0),
    )


def build_dashboard(
    store: SnapshotStore,
    *,
    top_n: int = 10,
    probe_execution: bool = True,
) -> DashboardSummary:
    """Assemble everything an operations view needs, in one read."""
    network = store.network()
    merchants = store.merchants()
    scenarios = store.scenarios()

    vulnerable = [m for m in merchants if m.vulnerable]
    exposed = sum(m.payables_in_horizon for m in vulnerable)

    summaries: list[ScenarioSummary] = store.scenario_summaries()
    scored = network.n_merchants or 1
    failure_rates = [s.projected_impact.n_affected / scored for s in scenarios]

    opportunities = [
        s for s in scenarios if s.recommended_intervention is not None
    ]
    efficiencies = [
        s.counterfactual.capital_efficiency
        for s in opportunities
        if s.counterfactual.capital_efficiency is not None
    ]

    return DashboardSummary(
        network=network,
        merchants_vulnerable=len(vulnerable),
        vulnerable_share=len(vulnerable) / scored,
        total_value_exposed=exposed,
        top_systemic=store.systemic().entries[:top_n],
        top_dependencies=store.dependencies()[:top_n],
        recent_scenarios=summaries,
        mean_failure_probability=(
            sum(failure_rates) / len(failure_rates) if failure_rates else None
        ),
        projected_disrupted_value=sum(
            s.projected_impact.disrupted_value for s in scenarios
        ),
        intervention_opportunities=len(opportunities),
        best_capital_efficiency=max(efficiencies) if efficiencies else None,
        total_recommended_capital=sum(
            s.recommended_intervention.liquidity_required
            for s in opportunities
            if s.recommended_intervention is not None
        ),
        recommended_offer=_best_offer(store),
        execution=execution_status(probe=probe_execution),
        provenance=store.provenance,
    )


def dashboard_payload(store: SnapshotStore, **kwargs: Any) -> dict[str, Any]:
    return build_dashboard(store, **kwargs).model_dump()
