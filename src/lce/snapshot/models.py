"""The frontend data contract.

Everything a dashboard needs, as plain serialisable records with no simulator
concepts in them. A client reading these should never need to know that a
discrete-event simulator exists, what a tick is, or what an obligation's
``issued_t`` means relative to a horizon.

Two rules shaped these types:

**A scenario is a result, not a session.** There is no timeline, no cursor, no
play state, no "current time". A scenario is an analytical snapshot: this shock,
these merchants affected, this projected impact, this recommendation, this
counterfactual. The backend runs an event-driven simulation to produce it; that
is an implementation detail and does not appear in the contract.

**Nothing here is a claim about a financial product.** An
:class:`OfferContract` is a *decision recommendation* with an explicit status of
``recommended`` and a disclaimer field that travels with it. It is not an
approval, not an underwriting decision, and not a quote. The naming is
deliberate: ``indicative_cost`` and ``indicative_rate_annual_pct`` rather than
``price`` and ``apr``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SNAPSHOT_FORMAT_VERSION = 1

#: Attached to every offer. The system recommends; it does not underwrite.
OFFER_DISCLAIMER = (
    "Decision recommendation produced by a network model on synthetic benchmark "
    "data. Not a credit approval, not an underwriting decision, and not a "
    "financial product offer. Indicative amounts and costs are model outputs "
    "under stated assumptions, not quoted terms."
)


class Contract(BaseModel):
    """Base for every response model: immutable, no surprise fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


# ------------------------------------------------------------------ provenance


class Provenance(Contract):
    """What produced a number, and how to reproduce it.

    Carried on every analytical result rather than only at the top level: a
    client that renders one scenario should be able to cite that scenario's
    lineage without holding the whole snapshot.
    """

    run_id: str
    scenario_id: str | None = None
    dataset_id: str
    dataset_version: str
    seed: int
    config_hash: str
    model_version: str | None = None
    feature_schema_version: str | None = None
    simulator_config_hash: str
    optimizer: str
    code_version: str
    created_at: str


# --------------------------------------------------------------------- network


class NetworkOverview(Contract):
    """Size and shape of the network, and how much value sits in it."""

    dataset_id: str
    dataset_version: str
    scale: str
    n_merchants: int
    n_relationships: int = Field(
        description=(
            'Ordered merchant pairs that transacted at least once in the '
            'observed history. This is the *payment* layer; the estimated '
            'dependency overlay served by /network/dependencies is a '
            'separate set over the same pairs and is counted separately.'
        )
    )
    n_payment_events: int
    n_obligations: int
    total_payment_value: float
    total_obligation_value: float
    obligation_value_in_horizon: float
    horizon_hours: float
    currency: str = "INR"
    sectors: dict[str, int] = Field(default_factory=dict)
    tiers: dict[str, int] = Field(default_factory=dict)


class MerchantView(Contract):
    """One merchant: who it is, what it holds, what it owes, how exposed it is."""

    merchant_id: str
    sector: str
    tier: str
    opening_balance: float
    credit_limit: float
    operating_floor: float
    liquidity_buffer: float
    payables_in_horizon: float
    receivables_in_horizon: float
    net_position: float
    cover_ratio: float | None = Field(
        default=None,
        description=(
            "(buffer + receivables due) / payables due. Below 1.0 means the "
            "merchant cannot clear its book from its own resources. None when it "
            "owes nothing in the horizon."
        ),
    )
    throughput: float = Field(description="Observed payment value through this merchant.")
    in_degree: int
    out_degree: int
    systemic_importance: float | None = None
    systemic_rank: int | None = None
    vulnerable: bool = Field(
        description="Cover ratio below 1.0 - short on its own resources for the horizon."
    )


class DependencyView(Contract):
    """One estimated relationship. Strength is inferred, never observed directly."""

    source_id: str
    target_id: str
    pass_through: float = Field(description="Estimated share of an inflow forwarded on.")
    conditional_probability: float
    lag_mean_hours: float
    reliability: float
    observed_value: float = Field(description="Total payment value seen on this link.")
    n_events: int
    estimated: bool = True


class SystemicEntry(Contract):
    """A merchant's systemic importance, with the baselines it must not be."""

    merchant_id: str
    rank: int
    importance: float = Field(description="Normalised marginal disruption, 0-1.")
    marginal_disruption: float
    scale_normalised: float = Field(
        description="Marginal disruption per unit of the merchant's own scale."
    )
    downstream_affected: int
    downstream_delayed_value: float
    cascade_depth: int
    time_to_impact_hours: float | None
    throughput: float
    structural_centrality: float


class SystemicRankingView(Contract):
    """The ranking, plus the correlations that say what it is not."""

    entries: list[SystemicEntry]
    n_sampled: int
    n_merchants: int
    shock_fraction: float
    baseline_rank_correlation: dict[str, float] = Field(
        description=(
            "Spearman correlation against throughput, degree and cash deficit. A "
            "high throughput correlation means the ranking is substantially a "
            "size ranking; it is reported rather than assumed away."
        )
    )
    method: str
    provenance: Provenance | None = Field(
        default=None,
        description=(
            'The run that measured this ranking. Optional only so snapshots '
            'written before it existed still load; the API always sets it.'
        ),
    )


# -------------------------------------------------------------------- scenario


class ShockView(Contract):
    """What went wrong, in business terms."""

    description: str
    origin_merchants: list[str]
    magnitude: float
    onset_hours: float
    kind: str
    family: str


class AffectedMerchant(Contract):
    """One merchant the shock reaches, and how hard."""

    merchant_id: str
    rank: int
    probability_constrained: float | None = Field(
        default=None,
        description="Model probability where a calibrated model produced one.",
    )
    time_to_constraint_hours: float | None
    disrupted_value: float
    cascade_depth: int | None
    sector: str
    tier: str


class TimeToConstraintBucket(Contract):
    """One bar of the time-to-constraint distribution."""

    from_hours: float
    to_hours: float
    n_merchants: int
    disrupted_value: float
    cumulative_share: float


class ProjectedImpact(Contract):
    """What the shock does to the network if nothing is done."""

    n_affected: int
    n_defaulted: int
    disrupted_value: float
    disruption_index: float = Field(
        description=(
            "Disruption *caused by this shock*: the objective under the shock "
            "minus the objective on the undisturbed network. A comparative index "
            "in model units, not a rupee total - use disrupted_value for money. "
            "Attributable by construction, so a shock that reaches nobody scores "
            "near zero here even on a network with standing distress."
        )
    )
    network_disruption_index: float = Field(
        description=(
            "The whole network's disruption under the shock, including the "
            "background lateness it carries anyway. The denominator the "
            "counterfactual's reduction percentage is measured against."
        )
    )
    max_cascade_depth: int
    systemic_exposure: float = Field(
        description="Share of network obligation value held by affected merchants."
    )


class Confidence(Contract):
    """How much to trust the projection, and why."""

    source: Literal["propagation", "artifact"]
    calibrated: bool
    model_version: str | None = None
    robust_mode: bool
    n_scenarios_considered: int
    disruption_spread: float | None = Field(
        default=None,
        description="Standard deviation of disruption across perturbed worlds.",
    )
    recommendation_stable_under_uncertainty: bool | None = Field(
        default=None,
        description="Whether accounting for uncertainty changed the recommendation.",
    )
    note: str = ""


class InterventionOption(Contract):
    """One action the backend would consider, with its measured effect."""

    intervention_id: str
    type: str
    merchant_id: str
    liquidity_required: float = Field(
        description="Capital the action needs. Zero for term-structure actions."
    )
    amount: float
    duration_hours: float = Field(
        description="How long the action is in effect: term shift, or restructure span."
    )
    apply_at_hours: float
    cost: float
    predicted_downstream_disruption: float = Field(
        description="Obligation value downstream of the target, at risk in the horizon."
    )
    disruption_prevented: float
    capital_efficiency: float | None = Field(
        description="Disruption prevented per rupee. None when the action is free."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    feasible: bool
    constraint_violations: list[str] = Field(default_factory=list)
    rationale: dict[str, float] = Field(
        default_factory=dict,
        description="The measurable factors that scored this action. No narrative.",
    )
    selected: bool = False


class CounterfactualView(Contract):
    """Before and after, both measured by replaying the action.

    ``baseline_disruption`` is the *whole network's* disruption under the shock,
    which is what an intervention actually acts on - it can relieve pre-existing
    distress as well as the shock's own damage. ``attributable_disruption`` is
    the shock's own contribution. Both are reported because a reduction
    percentage against the first is not a statement about the second, and
    collapsing them would let a scenario that reaches nobody appear to have been
    substantially mitigated.
    """

    baseline_disruption: float
    attributable_disruption: float
    baseline_disrupted_value: float
    baseline_affected: int
    with_intervention_disruption: float
    with_intervention_disrupted_value: float
    with_intervention_affected: int
    disruption_prevented: float
    disruption_reduction_pct: float
    commerce_preserved: float
    merchants_protected: int
    cost: float
    capital_efficiency: float | None
    optimality_gap: float | None = Field(
        default=None,
        description=(
            "Relative gap to a complete-enumeration optimum, where one was "
            "affordable to compute. None at scales where none was."
        ),
    )
    regret: float | None = None


class RepaymentTerms(Contract):
    structure: Literal["bullet", "instalments"]
    n_instalments: int
    instalment_amount: float
    first_due_hours: float
    cadence_hours: float


class OfferEligibility(Contract):
    eligible: bool
    criteria: dict[str, bool]
    constraints: dict[str, float | int | None]


class OfferContract(Contract):
    """A structured recommendation for one merchant. Not a financial product.

    ``status`` is always ``recommended``. There is no code path that advances it
    to approved or issued, because nothing in this system underwrites anything.
    """

    offer_id: str
    merchant_id: str
    scenario_id: str
    intervention_type: str
    proposed_amount: float
    currency: str = "INR"
    duration_hours: float
    repayment: RepaymentTerms
    indicative_cost: float
    indicative_rate_annual_pct: float | None = Field(
        default=None,
        description=(
            "Cost annualised over the stated duration, for comparability only. "
            "Derived from the configured fee rates, which are declared "
            "assumptions rather than quoted pricing."
        ),
    )
    rationale: str
    expected_network_benefit: dict[str, float]
    eligibility: OfferEligibility
    status: Literal["recommended"] = "recommended"
    disclaimer: str = OFFER_DISCLAIMER
    provenance: Provenance


class ScenarioSnapshot(Contract):
    """One analysed shock. A result, not a session."""

    scenario_id: str
    family: str
    shock: ShockView
    projected_impact: ProjectedImpact
    confidence: Confidence
    affected_merchants: list[AffectedMerchant]
    time_to_constraint: list[TimeToConstraintBucket]
    recommended_intervention: InterventionOption | None
    alternatives: list[InterventionOption]
    counterfactual: CounterfactualView
    offer: OfferContract | None
    provenance: Provenance
    computed_in_ms: float


class ScenarioSummary(Contract):
    """Enough to render a scenario in a list without fetching the whole thing."""

    scenario_id: str
    family: str
    headline: str
    n_affected: int
    disrupted_value: float
    max_cascade_depth: int
    recommended_action: str | None
    cost: float
    disruption_reduction_pct: float
    capital_efficiency: float | None


# ------------------------------------------------------------------ dashboard


class ExecutionStatus(Contract):
    """What the payment provider can actually do right now."""

    provider: str
    mode: str
    configured: bool
    api_reachable: bool
    capabilities: dict[str, bool]
    executable_intervention_types: list[str]
    fallback_provider: str
    note: str


class DashboardSummary(Contract):
    """The admin data contract, computed - never hardcoded."""

    network: NetworkOverview
    merchants_vulnerable: int
    vulnerable_share: float
    total_value_exposed: float = Field(
        description="Obligation value held by merchants with cover ratio below 1."
    )
    top_systemic: list[SystemicEntry]
    top_dependencies: list[DependencyView]
    recent_scenarios: list[ScenarioSummary]
    mean_failure_probability: float | None
    projected_disrupted_value: float
    intervention_opportunities: int
    best_capital_efficiency: float | None
    total_recommended_capital: float
    recommended_offer: OfferContract | None
    execution: ExecutionStatus
    provenance: Provenance


class SnapshotManifest(Contract):
    """Identity and integrity of a stored snapshot."""

    snapshot_id: str
    format_version: int = SNAPSHOT_FORMAT_VERSION
    created_at: str
    code_version: str
    dataset_version: str
    scale: str
    seed: int
    n_scenarios: int
    content_hash: str
    provenance: Provenance
    build: dict[str, Any] = Field(default_factory=dict)
