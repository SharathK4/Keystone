"""Merchant nodes and their liquidity state.

Mathematical definitions
------------------------
For merchant :math:`i` at simulation time :math:`t`:

.. math::

    L_i(t)  \\;=\\; \\text{cash balance}

    K_i(t)  \\;=\\; \\text{undrawn credit line}

    \\underline{L}_i \\;=\\; \\text{operating floor (minimum working cash)}

    b_i(t)  \\;=\\; L_i(t) + K_i(t) - \\underline{L}_i
              \\quad\\text{(liquidity buffer / headroom)}

The buffer :math:`b_i(t)` is *the* state variable contagion acts on: a node is
**constrained** at :math:`t` when an obligation of size :math:`a` falls due and
:math:`b_i(t) < a`.

Balance evolution over :math:`[t, t+\\Delta)`:

.. math::

    L_i(t+\\Delta) = L_i(t)
        + \\underbrace{\\textstyle\\sum_{e \\in A_i[t,t+\\Delta)} a_e}_{\\text{network inflows}}
        + \\underbrace{\\lambda^{in}_i \\Delta}_{\\text{exogenous revenue}}
        - \\underbrace{\\textstyle\\sum_{e \\in B_i[t,t+\\Delta)} a_e}_{\\text{network outflows}}
        - \\underbrace{\\mu_i \\Delta}_{\\text{operating burn}}
        - \\underbrace{S_i(t)}_{\\text{shock}}

where :math:`A_i` and :math:`B_i` are the marked point processes of received and
made payments respectively.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, computed_field, model_validator

from lce.domain.base import DomainModel, MerchantId, new_id, utcnow
from lce.domain.enums import MerchantSector, MerchantTier, NodeStatus


class MerchantProfile(DomainModel):
    """Static / slowly-varying attributes of a merchant node.

    These are the parameters of the node's cash process, not its state. State
    lives in :class:`LiquidityState`.
    """

    merchant_id: MerchantId = Field(default_factory=lambda: new_id("mrc"))
    external_id: str | None = Field(
        default=None, description="Razorpay account/contact id when sourced from live data."
    )
    name: str = ""
    sector: MerchantSector = MerchantSector.OTHER
    tier: MerchantTier = MerchantTier.SMALL

    # --- cash-process parameters -------------------------------------------
    opening_balance: float = Field(
        ge=0.0, description="L_i(0), opening cash balance in INR."
    )
    operating_floor: float = Field(
        default=0.0, ge=0.0, description="L-underbar_i: minimum working cash the node must hold."
    )
    credit_limit: float = Field(
        default=0.0, ge=0.0, description="K_i: total sanctioned credit line in INR."
    )
    exogenous_inflow_rate: float = Field(
        default=0.0, ge=0.0, description="lambda^in_i: off-network revenue, INR per hour."
    )
    operating_burn_rate: float = Field(
        default=0.0, ge=0.0, description="mu_i: operating outflow, INR per hour."
    )

    # --- behavioural parameters --------------------------------------------
    payment_discipline: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="Propensity to pay on time when liquidity allows (reliability prior).",
    )
    stress_threshold_ratio: float = Field(
        default=0.25,
        ge=0.0,
        description="Buffer/opening-balance ratio below which the node is flagged STRESSED.",
    )
    systemic_weight: float = Field(
        default=1.0,
        ge=0.0,
        description="w_i: societal/portfolio weight of this node in the disruption objective.",
    )

    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_floor(self) -> MerchantProfile:
        if self.operating_floor > self.opening_balance + self.credit_limit:
            raise ValueError(
                "operating_floor exceeds opening_balance + credit_limit: "
                "node would start already constrained"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def initial_buffer(self) -> float:
        """b_i(0) = L_i(0) + K_i - L-underbar_i."""
        return self.opening_balance + self.credit_limit - self.operating_floor

    @property
    def net_burn_rate(self) -> float:
        """mu_i - lambda^in_i: net off-network drain, INR per hour."""
        return self.operating_burn_rate - self.exogenous_inflow_rate

    def autonomy_hours(self) -> float:
        """How long the node survives on its buffer with no network inflows.

        ``inf`` when the node is exogenously cash-positive.
        """
        drain = self.net_burn_rate
        if drain <= 0:
            return float("inf")
        return self.initial_buffer / drain


class LiquidityState(DomainModel):
    """Instantaneous liquidity state of merchant ``i`` at time ``t``.

    Immutable snapshot. The simulator holds a mutable working copy
    (:class:`lce.simulation.state.NodeState`) and emits these for the record.
    """

    merchant_id: MerchantId
    t: float = Field(description="Simulation time in hours.")

    cash_balance: float = Field(description="L_i(t). May go negative (overdraft into credit).")
    credit_drawn: float = Field(default=0.0, ge=0.0, description="Portion of K_i already used.")
    credit_limit: float = Field(default=0.0, ge=0.0, description="K_i(t), post-shock.")
    operating_floor: float = Field(default=0.0, ge=0.0)

    pending_payable: float = Field(
        default=0.0, ge=0.0, description="Sum of unsettled obligations owed by i."
    )
    pending_receivable: float = Field(
        default=0.0, ge=0.0, description="Sum of unsettled obligations owed to i."
    )
    overdue_payable: float = Field(default=0.0, ge=0.0)

    status: NodeStatus = NodeStatus.HEALTHY

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available_credit(self) -> float:
        """K_i(t) - drawn."""
        return max(0.0, self.credit_limit - self.credit_drawn)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def buffer(self) -> float:
        """b_i(t) = L_i(t) + available credit - L-underbar_i."""
        return self.cash_balance + self.available_credit - self.operating_floor

    @property
    def deficit(self) -> float:
        """(L-underbar_i - L_i(t))^+ : shortfall against the operating floor."""
        return max(0.0, self.operating_floor - self.cash_balance)

    def can_pay(self, amount: float) -> bool:
        """Whether obligation of size ``amount`` is fully coverable right now."""
        return self.buffer >= amount

    def coverage_ratio(self) -> float:
        """b_i(t) / pending payables. < 1 means the node cannot clear its book."""
        if self.pending_payable <= 0:
            return float("inf")
        return self.buffer / self.pending_payable


class MerchantSnapshot(DomainModel):
    """Profile + state, the shape most API consumers want."""

    profile: MerchantProfile
    state: LiquidityState
