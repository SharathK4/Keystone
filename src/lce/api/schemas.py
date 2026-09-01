"""API request/response schemas.

Deliberately separate from the domain models. Domain objects are frozen value
types tuned for the maths; API schemas are a contract with clients and change
for different reasons (pagination, field naming, backwards compatibility).
Coupling them would mean a refactor of the simulator silently breaks callers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lce.domain.enums import (
    InterventionType,
    OptimizerKind,
    PredictorKind,
    ShockKind,
)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ------------------------------------------------------------------- health


class HealthResponse(APIModel):
    status: str = Field(description="ok | degraded | error")
    version: str
    environment: str
    database: dict[str, Any]
    razorpay: dict[str, Any]
    checks: dict[str, str] = Field(default_factory=dict)


class ReadinessResponse(APIModel):
    ready: bool
    detail: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ datasets


class GenerateNetworkRequest(APIModel):
    n_merchants: int = Field(default=60, ge=4, le=2000)
    n_layers: int = Field(default=4, ge=2, le=8)
    seed: int | None = Field(default=None, description="Defaults to RANDOM_SEED.")
    horizon_hours: float = Field(default=168.0, gt=0, le=24 * 90)
    history_hours: float = Field(default=1440.0, gt=0, le=24 * 365)
    mean_out_degree: float = Field(default=2.4, gt=0, le=20)
    coverage_low: float = Field(default=0.30, ge=0.01, le=5.0)
    coverage_high: float = Field(default=0.65, ge=0.01, le=5.0)
    notes: str = ""


class DatasetSummary(APIModel):
    dataset_id: str
    dataset_version: str
    source: str
    seed: int
    stats: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    notes: str = ""


class DatasetDetail(APIModel):
    dataset_id: str
    dataset_version: str
    source: str
    seed: int
    config: dict[str, Any] = Field(default_factory=dict)
    stats: dict[str, Any] = Field(default_factory=dict)
    graph: dict[str, Any] = Field(default_factory=dict)


# ----------------------------------------------------------------- merchants


class MerchantSummary(APIModel):
    merchant_id: str
    name: str
    sector: str
    tier: str
    opening_balance: float
    operating_floor: float
    credit_limit: float
    initial_buffer: float
    systemic_weight: float
    payment_discipline: float


class MerchantDetail(MerchantSummary):
    exogenous_inflow_rate: float
    operating_burn_rate: float
    autonomy_hours: float | None
    n_suppliers: int
    n_buyers: int
    payables_in_horizon: float
    receivables_in_horizon: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class PagedMerchants(APIModel):
    items: list[MerchantSummary]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------- graph


class EdgeView(APIModel):
    source_id: str
    target_id: str
    pass_through: float
    conditional_probability: float
    reliability: float
    mean_lag_hours: float
    n_events: int
    mean_amount: float
    estimator: str | None
    is_ground_truth: bool
    confidence: float


class GraphView(APIModel):
    dataset_id: str
    stats: dict[str, Any]
    nodes: list[MerchantSummary]
    edges: list[EdgeView]


class LearnDependenciesRequest(APIModel):
    t_end: float = Field(default=0.0, description="Hold out events at or after this time.")
    em_iterations: int = Field(default=40, ge=1, le=500)
    max_parents_per_event: int = Field(default=64, ge=1, le=512)


# -------------------------------------------------------------------- shocks


class ShockComponentRequest(APIModel):
    merchant_id: str
    magnitude: float = Field(gt=0)
    t: float = Field(default=0.0, ge=0)
    kind: ShockKind = ShockKind.MISSED_INBOUND
    duration_hours: float = Field(default=0.0, ge=0)
    target_obligation_id: str | None = None


class CreateShockRequest(APIModel):
    name: str = ""
    description: str = ""
    components: list[ShockComponentRequest] = Field(min_length=1)


class ShockResponse(APIModel):
    shock_id: str
    name: str
    description: str
    total_magnitude: float
    onset_t: float
    origin_ids: list[str]
    components: list[dict[str, Any]]


# ---------------------------------------------------------------- simulation


class SimulateRequest(APIModel):
    shock_id: str | None = Field(
        default=None, description="Omit to run the undisturbed baseline."
    )
    plan_id: str | None = None
    horizon_hours: float | None = Field(default=None, gt=0, le=24 * 90)
    tick_hours: float | None = Field(default=None, gt=0, le=24)
    seed: int | None = None
    store_events: bool = True
    estimator: str | None = None


class NodeOutcomeView(APIModel):
    merchant_id: str
    is_affected: bool
    became_constrained: bool
    became_defaulted: bool
    first_constrained_t: float | None
    hop_distance: int | None
    value_delayed: float
    weighted_delay: float
    defaults_caused: int
    min_buffer: float
    final_status: str


class CascadeResponse(APIModel):
    run_id: str
    shock_id: str | None
    plan_id: str | None
    horizon_hours: float
    disruption: float | None
    disruption_breakdown: dict[str, float]
    summary: dict[str, Any]
    affected_ids: list[str]
    downstream_affected_ids: list[str]
    timeline: dict[str, list[str]] = Field(
        default_factory=dict, description="Affected set at 6h / 24h / 48h / 72h."
    )
    outcomes: list[NodeOutcomeView] = Field(default_factory=list)


class PropagationEventView(APIModel):
    event_id: str
    sequence: int
    t: float
    type: str
    merchant_id: str
    counterparty_id: str | None
    amount: float
    hop: int
    caused_by: str | None
    status_after: str | None


# ---------------------------------------------------------------- prediction


class PredictRequest(APIModel):
    shock_id: str
    predictor: PredictorKind = PredictorKind.LINEAR_THRESHOLD
    horizon_hours: float = Field(default=168.0, gt=0, le=24 * 90)
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    max_hops: int = Field(default=6, ge=1, le=20)
    estimator: str | None = None


class NodeExposureView(APIModel):
    merchant_id: str
    exposure_score: float
    expected_shortfall: float
    expected_hit_t: float | None
    hop_distance: int | None
    contributing_sources: list[str] = Field(default_factory=list)


class PredictionResponse(APIModel):
    prediction_id: str
    shock_id: str | None
    predictor: str
    model_version: str
    horizon_hours: float
    threshold: float
    inference_ms: float | None
    predicted_affected_ids: list[str]
    exposures: list[NodeExposureView]

    model_config = ConfigDict(extra="forbid", protected_namespaces=())


# -------------------------------------------------------------- intervention


class OptimizeRequest(APIModel):
    shock_id: str
    optimizer: OptimizerKind = OptimizerKind.GREEDY
    budget: float | None = Field(default=None, ge=0)
    max_actions: int = Field(default=3, ge=1, le=10)
    top_k_nodes: int = Field(default=8, ge=1, le=50)
    max_candidates: int = Field(default=120, ge=1, le=1000)
    horizon_hours: float = Field(default=168.0, gt=0, le=24 * 90)
    lazy: bool = False
    estimator: str | None = None


class InterventionView(APIModel):
    intervention_id: str
    type: InterventionType
    merchant_id: str
    t: float
    amount: float
    shift_hours: float
    tranches: int
    target_obligation_id: str | None
    cost: float
    description: str


class OptimizeResponse(APIModel):
    plan_id: str
    optimizer: str
    interventions: list[InterventionView]
    total_cost: float
    baseline_disruption: float
    achieved_disruption: float
    disruption_prevented: float
    disruption_prevented_per_rupee: float | None
    candidates_considered: int
    simulations_run: int
    elapsed_ms: float
    notes: dict[str, Any] = Field(default_factory=dict)


class RankedInterventionsResponse(APIModel):
    shock_id: str
    ranked: list[dict[str, Any]]


# --------------------------------------------------------------- evaluation


class EvaluateRequest(APIModel):
    shock_id: str
    prediction_id: str
    horizon_hours: float = Field(default=168.0, gt=0, le=24 * 90)
    estimator: str | None = None


class EvaluationResponse(APIModel):
    evaluation_id: str
    name: str
    predictor: str | None
    optimizer: str | None
    headline: dict[str, Any]
    by_horizon: dict[str, dict[str, float]] = Field(default_factory=dict)


class SystemicImportanceRequest(APIModel):
    shock_fraction: float = Field(default=1.5, gt=0, le=20)
    limit: int | None = Field(default=None, ge=1, le=2000)
    estimator: str | None = None


class SystemicImportanceResponse(APIModel):
    dataset_id: str
    baseline_disruption: float
    shock_fraction: float
    ranking: list[dict[str, Any]]


# --------------------------------------------------------------------- runs


class RunView(APIModel):
    run_id: str
    kind: str
    status: str
    name: str
    dataset_version: str | None
    model_version: str | None
    shock_id: str | None
    plan_id: str | None
    seed: int
    config_hash: str | None
    metrics: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None
    created_at: str | None
    git_sha: str | None

    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class PagedRuns(APIModel):
    items: list[RunView]
    limit: int
    offset: int


# ----------------------------------------------------------------- webhooks


class WebhookAck(APIModel):
    status: str
    event_id: str
    ingested: int = 0
    reason: str | None = None


class ErrorResponse(APIModel):
    code: str
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
