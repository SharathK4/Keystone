"""Request and response shapes for the versioned inference API.

Kept in the inference package rather than with the research API's schemas so the
serving contract travels with the thing that serves it. A caller integrating
against ``/api/v1`` should need this module and the artifact, nothing else.

Validation is strict on purpose. A malformed network posted to a prediction
endpoint produces numbers - wrong ones - if the fields are merely defaulted, so
required fields are required and unknown ones are rejected rather than silently
ignored.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lce.domain.enums import InterventionType, MerchantSector, MerchantTier, ShockKind


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MerchantStateIn(_Strict):
    """A merchant as a caller can observe it.

    The latent behaviour parameters are deliberately absent from this schema.
    The model was fitted on scrubbed profiles, so accepting an exogenous inflow
    rate here would let a caller feed the model a column it was never trained
    on - and the resulting prediction would look perfectly ordinary.
    """

    merchant_id: str = Field(min_length=1)
    sector: MerchantSector = MerchantSector.OTHER
    tier: MerchantTier = MerchantTier.SMALL
    opening_balance: float = Field(ge=0.0, description="L_i(0), INR.")
    credit_limit: float = Field(default=0.0, ge=0.0, description="K_i, INR.")
    operating_floor: float = Field(default=0.0, ge=0.0, description="Minimum working cash.")

    @model_validator(mode="after")
    def _floor_is_reachable(self) -> MerchantStateIn:
        if self.operating_floor > self.opening_balance + self.credit_limit:
            raise ValueError(
                f"{self.merchant_id}: operating_floor exceeds opening_balance + "
                "credit_limit, so the merchant starts already constrained"
            )
        return self


class ObligationIn(_Strict):
    obligation_id: str = Field(min_length=1)
    debtor_id: str = Field(min_length=1)
    creditor_id: str = Field(min_length=1)
    amount: float = Field(gt=0.0)
    issued_t: float
    due_t: float
    priority: int = 0
    amount_paid: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> ObligationIn:
        if self.due_t < self.issued_t:
            raise ValueError(f"{self.obligation_id}: due_t precedes issued_t")
        if self.debtor_id == self.creditor_id:
            raise ValueError(f"{self.obligation_id}: debtor and creditor are the same")
        return self


class PaymentIn(_Strict):
    payer_id: str = Field(min_length=1)
    payee_id: str = Field(min_length=1)
    amount: float = Field(gt=0.0)
    t: float
    obligation_id: str | None = None

    @model_validator(mode="after")
    def _no_self_payment(self) -> PaymentIn:
        if self.payer_id == self.payee_id:
            raise ValueError("a merchant cannot pay itself")
        return self


class NetworkStateIn(_Strict):
    network_id: str = "request"
    merchants: list[MerchantStateIn] = Field(min_length=1)
    obligations: list[ObligationIn] = Field(default_factory=list)
    payments: list[PaymentIn] = Field(default_factory=list)


class ShockComponentIn(_Strict):
    merchant_id: str = Field(min_length=1)
    magnitude: float = Field(gt=0.0)
    t: float = Field(ge=0.0)
    kind: ShockKind = ShockKind.MISSED_INBOUND


class ShockIn(_Strict):
    name: str = "request"
    components: list[ShockComponentIn] = Field(min_length=1)


class ContagionRequest(_Strict):
    """``POST /api/v1/predict/contagion``."""

    network: NetworkStateIn
    shock: ShockIn
    observation_cutoff: float = Field(
        default=0.0,
        description=(
            "Nothing at or after this time is used. Enforced server-side: a "
            "caller cannot obtain a prediction from information the model was "
            "not allowed to see when it was fitted."
        ),
    )
    horizon_hours: float = Field(default=168.0, gt=0.0)
    model_version: str | None = Field(
        default=None, description="Pin a specific artifact; omit for the loaded one."
    )

    @model_validator(mode="after")
    def _window_is_non_empty(self) -> ContagionRequest:
        if self.horizon_hours <= self.observation_cutoff:
            raise ValueError("horizon_hours must be after observation_cutoff")
        return self


class NodePredictionOut(BaseModel):
    merchant_id: str
    probability_constrained: float
    expected_time_to_constraint_hours: float | None
    probability_by: dict[str, float]


class ContagionResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_version: str
    feature_schema_version: str
    artifact_hash: str
    calibrator: str
    threshold: float
    observation_cutoff: float
    horizon_hours: float
    interval_edges: list[float]
    n_flagged: int
    nodes: list[NodePredictionOut]
    latency_ms: float


class ConstraintsIn(_Strict):
    """The feasible set a caller is willing to act inside."""

    budget: float | None = Field(default=None, ge=0.0)
    max_actions: int = Field(default=2, ge=0, le=8)
    max_per_merchant: int = Field(default=1, ge=1, le=4)
    max_extension_hours: float = Field(default=120.0, ge=0.0)
    max_acceleration_hours: float = Field(default=168.0, ge=0.0)
    max_tranches: int = Field(default=4, ge=1, le=24)
    min_amount: float = Field(default=1000.0, ge=0.0)


class ObjectiveIn(_Strict):
    form: Literal["penalised", "constrained"] = "penalised"
    lam: float = Field(default=1.0, ge=0.0, alias="lambda")
    epsilon: float | None = Field(default=None, ge=0.0)
    epsilon_fraction: float = Field(default=0.5, ge=0.0, le=1.0)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RecommendRequest(_Strict):
    """``POST /api/v1/interventions/recommend``."""

    network: NetworkStateIn
    shock: ShockIn
    observation_cutoff: float = 0.0
    horizon_hours: float = Field(default=168.0, gt=0.0)
    constraints: ConstraintsIn = Field(default_factory=ConstraintsIn)
    objective: ObjectiveIn = Field(default_factory=ObjectiveIn)
    max_candidates: int = Field(default=12, ge=1, le=64)
    robust: bool = False
    n_scenarios: int = Field(default=3, ge=1, le=16)
    seed: int = 20250101


class InterventionOut(BaseModel):
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
    explanation: dict[str, Any]


class RecommendResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_version: str
    selected: list[InterventionOut]
    ranked: list[dict[str, Any]]
    baseline_disruption: float
    residual_disruption: float
    expected_disruption_reduction: float
    cost: float
    capital_efficiency: float | None
    robustness: dict[str, Any]
    feasibility: dict[str, Any]
    candidates: dict[str, Any]
    solver: dict[str, Any]
    latency_ms: float


class InterventionIn(_Strict):
    """An action to replay. Same shape the recommender returns."""

    type: InterventionType
    merchant_id: str = Field(min_length=1)
    t: float = Field(ge=0.0)
    amount: float = Field(default=0.0, ge=0.0)
    shift_hours: float = Field(default=0.0, ge=0.0)
    tranches: int = Field(default=1, ge=1, le=24)
    target_obligation_id: str | None = None


class ReplayRequest(_Strict):
    """``POST /api/v1/scenarios/replay``."""

    network: NetworkStateIn
    shock: ShockIn
    interventions: list[InterventionIn] = Field(default_factory=list)
    horizon_hours: float = Field(default=168.0, gt=0.0)
    seed: int = 20250101


class ReplayResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_version: str
    before: dict[str, Any]
    after: dict[str, Any]
    disruption_prevented: float
    disruption_reduction_pct: float
    commerce_preserved: float
    cost: float
    capital_efficiency: float | None
    n_interventions: int
    latency_ms: float
