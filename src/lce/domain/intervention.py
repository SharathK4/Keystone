"""Interventions: the control variables of the optimisation problem.

An intervention :math:`u` is applied to a node (and sometimes a specific
obligation) at time :math:`t_u`. The optimiser chooses a set
:math:`U \\subseteq \\mathcal{U}` solving

.. math::

    \\min_{U} \\; D(G, S, U)
    \\quad \\text{s.t.} \\quad
    \\sum_{u \\in U} c(u) \\le B, \\;\\; |U| \\le k

where :math:`D` is the network disruption objective and :math:`c(u)` the
*deployed capital cost* of the intervention.

Cost model (all costs in INR, all rates configurable per candidate)
-------------------------------------------------------------------
``LIQUIDITY_INJECTION``      c = amount                        (capital at risk)
``CREDIT_LINE_INCREASE``     c = amount * utilisation_prior    (expected draw)
``RECEIVABLE_ACCELERATION``  c = amount * discount_rate * days (factoring fee)
``SUPPLIER_TERM_EXTENSION``  c = amount * carry_rate * days    (fee to supplier)
``REPAYMENT_RESTRUCTURE``    c = amount * restructure_fee_rate (admin + interest)

The headline metric the demo reports is **disruption prevented per rupee**:

.. math::

    \\mathrm{DPR}(U) = \\frac{D(G,S,\\emptyset) - D(G,S,U)}{\\sum_{u \\in U} c(u)}
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import Field, computed_field, model_validator

from lce.domain.base import DomainModel, MerchantId, ObligationId, new_id, utcnow
from lce.domain.enums import InterventionType

# Default pricing. Overridable per candidate; recorded in every run manifest.
DEFAULT_DISCOUNT_RATE_PER_DAY = 0.0005      # 0.05%/day factoring fee
DEFAULT_CARRY_RATE_PER_DAY = 0.0004         # 0.04%/day supplier carry fee
DEFAULT_RESTRUCTURE_FEE_RATE = 0.01         # 1% of restructured principal
DEFAULT_CREDIT_UTILISATION_PRIOR = 0.6      # expected draw on a new credit line
HOURS_PER_DAY = 24.0


class Intervention(DomainModel):
    """A single candidate action."""

    intervention_id: str = Field(default_factory=lambda: new_id("itv"))
    type: InterventionType
    merchant_id: MerchantId = Field(description="Node the intervention is applied to.")
    t: float = Field(ge=0.0, description="t_u, when the intervention takes effect.")

    amount: float = Field(
        default=0.0,
        ge=0.0,
        description="Principal moved: injected cash, accelerated receivable, or extended payable.",
    )
    shift_hours: float = Field(
        default=0.0,
        ge=0.0,
        description="Deadline shift for acceleration (earlier) / extension (later).",
    )
    target_obligation_id: ObligationId | None = Field(
        default=None, description="Obligation acted on, when the type requires one."
    )
    tranches: int = Field(
        default=1, ge=1, le=24, description="Number of instalments for a restructure."
    )
    tranche_spacing_hours: float = Field(
        default=HOURS_PER_DAY * 7, gt=0.0, description="Gap between restructure instalments."
    )

    # --- pricing -----------------------------------------------------------
    discount_rate_per_day: float = Field(default=DEFAULT_DISCOUNT_RATE_PER_DAY, ge=0.0)
    carry_rate_per_day: float = Field(default=DEFAULT_CARRY_RATE_PER_DAY, ge=0.0)
    restructure_fee_rate: float = Field(default=DEFAULT_RESTRUCTURE_FEE_RATE, ge=0.0)
    credit_utilisation_prior: float = Field(
        default=DEFAULT_CREDIT_UTILISATION_PRIOR, ge=0.0, le=1.0
    )

    label: str = ""
    provenance: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Why this action was generated: which rule proposed it, which "
            "measurable factors scored it, and what it was sized from. Written by "
            "the Phase-4 candidate generator so a recommendation can be explained "
            "without re-deriving it."
        ),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        needs_obligation = {
            InterventionType.RECEIVABLE_ACCELERATION,
            InterventionType.SUPPLIER_TERM_EXTENSION,
            InterventionType.REPAYMENT_RESTRUCTURE,
        }
        if self.type in needs_obligation and self.target_obligation_id is None:
            raise ValueError(f"{self.type} requires target_obligation_id")
        needs_amount = {
            InterventionType.LIQUIDITY_INJECTION,
            InterventionType.CREDIT_LINE_INCREASE,
        }
        if self.type in needs_amount and self.amount <= 0:
            raise ValueError(f"{self.type} requires a positive amount")
        needs_shift = {
            InterventionType.RECEIVABLE_ACCELERATION,
            InterventionType.SUPPLIER_TERM_EXTENSION,
        }
        if self.type in needs_shift and self.shift_hours <= 0:
            raise ValueError(f"{self.type} requires a positive shift_hours")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost(self) -> float:
        """c(u): capital deployed / fee paid, in INR."""
        days = self.shift_hours / HOURS_PER_DAY
        match self.type:
            case InterventionType.LIQUIDITY_INJECTION:
                return self.amount
            case InterventionType.CREDIT_LINE_INCREASE:
                return self.amount * self.credit_utilisation_prior
            case InterventionType.RECEIVABLE_ACCELERATION:
                return self.amount * self.discount_rate_per_day * days
            case InterventionType.SUPPLIER_TERM_EXTENSION:
                return self.amount * self.carry_rate_per_day * days
            case InterventionType.REPAYMENT_RESTRUCTURE:
                return self.amount * self.restructure_fee_rate
        raise AssertionError(f"unhandled intervention type {self.type}")

    @property
    def is_capital_deploying(self) -> bool:
        """Whether the cost is capital at risk rather than a fee."""
        return self.type in (
            InterventionType.LIQUIDITY_INJECTION,
            InterventionType.CREDIT_LINE_INCREASE,
        )

    def describe(self) -> str:
        if self.label:
            return self.label
        match self.type:
            case InterventionType.LIQUIDITY_INJECTION:
                return f"Inject INR {self.amount:,.0f} into {self.merchant_id} at t={self.t:.0f}h"
            case InterventionType.CREDIT_LINE_INCREASE:
                return f"Raise {self.merchant_id} credit line by INR {self.amount:,.0f}"
            case InterventionType.RECEIVABLE_ACCELERATION:
                return (
                    f"Accelerate receivable {self.target_obligation_id} to {self.merchant_id} "
                    f"by {self.shift_hours / HOURS_PER_DAY:.1f}d"
                )
            case InterventionType.SUPPLIER_TERM_EXTENSION:
                return (
                    f"Extend payable {self.target_obligation_id} from {self.merchant_id} "
                    f"by {self.shift_hours / HOURS_PER_DAY:.1f}d"
                )
            case InterventionType.REPAYMENT_RESTRUCTURE:
                return (
                    f"Restructure {self.target_obligation_id} into {self.tranches} tranches "
                    f"for {self.merchant_id}"
                )
        raise AssertionError(f"unhandled intervention type {self.type}")


class InterventionPlan(DomainModel):
    """A set of interventions evaluated together, with its measured effect."""

    plan_id: str = Field(default_factory=lambda: new_id("pln"))
    interventions: list[Intervention] = Field(default_factory=list)
    budget: float | None = Field(default=None, ge=0.0, description="B, if one was imposed.")
    max_actions: int | None = Field(default=None, ge=0, description="k, if one was imposed.")

    # Measured by counterfactual simulation; None until evaluated.
    baseline_disruption: float | None = None
    residual_disruption: float | None = None
    optimizer: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cost(self) -> float:
        """sum c(u) over the plan."""
        return sum(u.cost for u in self.interventions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disruption_prevented(self) -> float | None:
        """D(G,S,{}) - D(G,S,U)."""
        if self.baseline_disruption is None or self.residual_disruption is None:
            return None
        return self.baseline_disruption - self.residual_disruption

    @computed_field  # type: ignore[prop-decorator]
    @property
    def disruption_prevented_per_rupee(self) -> float | None:
        """DPR(U). ``None`` when unevaluated; ``inf`` for a free, effective plan."""
        prevented = self.disruption_prevented
        if prevented is None:
            return None
        if self.total_cost <= 0:
            return float("inf") if prevented > 0 else 0.0
        return prevented / self.total_cost

    @property
    def is_empty(self) -> bool:
        return not self.interventions

    def is_feasible(self) -> bool:
        if self.budget is not None and self.total_cost > self.budget + 1e-6:
            return False
        return not (self.max_actions is not None and len(self.interventions) > self.max_actions)

    def with_evaluation(self, baseline: float, residual: float, optimizer: str) -> InterventionPlan:
        return self.model_copy(
            update={
                "baseline_disruption": baseline,
                "residual_disruption": residual,
                "optimizer": optimizer,
            }
        )
