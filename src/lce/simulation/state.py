"""Mutable simulator state.

Domain objects are frozen values; the simulator needs a mutable working set, so
it lives here. :meth:`NodeState.snapshot` converts back into the immutable
:class:`~lce.domain.merchant.LiquidityState` for the record.

Cash accounting
---------------
``cash`` and ``credit_drawn`` are tracked separately rather than letting the
balance go negative into an implicit overdraft, because the two behave
differently under intervention: a ``CREDIT_LINE_INCREASE`` raises ``credit_limit``
without moving cash, while a ``LIQUIDITY_INJECTION`` does the opposite. Collapsing
them would make those two interventions indistinguishable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lce.domain.enums import NodeStatus
from lce.domain.merchant import LiquidityState, MerchantProfile


@dataclass(slots=True)
class PendingInflow:
    """Cash in flight: paid by ``source`` but not yet available to the payee."""

    available_t: float
    payee_id: str
    amount: float
    source_id: str
    source_event_id: str | None = None
    obligation_id: str | None = None


@dataclass(slots=True)
class NodeState:
    """Working liquidity state for one merchant during a run."""

    profile: MerchantProfile
    cash: float
    credit_drawn: float = 0.0
    credit_limit: float = 0.0
    operating_floor: float = 0.0
    exogenous_inflow_rate: float = 0.0
    operating_burn_rate: float = 0.0

    status: NodeStatus = NodeStatus.HEALTHY

    # --- accumulators feeding the objective and the outcome record ----------
    was_shocked: bool = False
    first_constrained_t: float | None = None
    first_defaulted_t: float | None = None
    hop_distance: int | None = None
    deficit_integral: float = 0.0
    min_buffer: float = field(default=float("inf"))
    weighted_delay: float = 0.0
    value_delayed: float = 0.0
    defaults_caused: int = 0
    obligations_missed: int = 0
    obligations_settled_late: int = 0

    # Event id of the most recent event that damaged this node - the causal
    # parent for anything this node goes on to break.
    last_impact_event_id: str | None = None

    @classmethod
    def from_profile(cls, profile: MerchantProfile) -> NodeState:
        return cls(
            profile=profile,
            cash=profile.opening_balance,
            credit_limit=profile.credit_limit,
            operating_floor=profile.operating_floor,
            exogenous_inflow_rate=profile.exogenous_inflow_rate,
            operating_burn_rate=profile.operating_burn_rate,
            min_buffer=profile.opening_balance + profile.credit_limit - profile.operating_floor,
        )

    @property
    def merchant_id(self) -> str:
        return self.profile.merchant_id

    @property
    def available_credit(self) -> float:
        return max(0.0, self.credit_limit - self.credit_drawn)

    @property
    def buffer(self) -> float:
        """b_i(t) = cash + undrawn credit - operating floor."""
        return self.cash + self.available_credit - self.operating_floor

    @property
    def deficit(self) -> float:
        return max(0.0, self.operating_floor - self.cash)

    def can_pay(self, amount: float) -> bool:
        return self.buffer >= amount

    def max_payable(self) -> float:
        """Largest payment the node can make without breaching its floor."""
        return max(0.0, self.buffer)

    # --- mutations ----------------------------------------------------------

    def receive(self, amount: float) -> None:
        """Take in cash, using it to repay drawn credit first."""
        if amount <= 0:
            return
        repay = min(self.credit_drawn, amount)
        self.credit_drawn -= repay
        self.cash += amount - repay

    def disburse(self, amount: float) -> float:
        """Pay out ``amount``, drawing on the credit line if cash is short.

        Returns the amount actually paid (may be less if the line is exhausted).
        """
        if amount <= 0:
            return 0.0
        payable = min(amount, self.cash + self.available_credit)
        if payable <= 0:
            return 0.0
        if payable > self.cash:
            draw = payable - self.cash
            self.credit_drawn += draw
            self.cash += draw
        self.cash -= payable
        return payable

    def accrue(self, dt: float) -> float:
        """Apply exogenous inflow and operating burn over ``dt`` hours.

        Operating burn is non-discretionary: it is allowed to push the node
        below its floor and into overdraft, which is exactly what the deficit
        term of the objective is measuring.
        """
        net = (self.exogenous_inflow_rate - self.operating_burn_rate) * dt
        if net >= 0:
            self.receive(net)
        else:
            self.cash += net
        return net

    def drain(self, amount: float) -> float:
        """Remove cash directly (a shock). May push the balance negative."""
        if amount <= 0:
            return 0.0
        self.cash -= amount
        return amount

    def observe(self, t: float, dt: float) -> None:
        """Record time-integrated statistics for the tick just completed."""
        self.deficit_integral += self.deficit * dt
        self.min_buffer = min(self.min_buffer, self.buffer)

    def mark_constrained(self, t: float, hop: int | None = None) -> bool:
        """Flag the node as constrained. Returns True on the first transition."""
        first = self.first_constrained_t is None
        if first:
            self.first_constrained_t = t
        if hop is not None and (self.hop_distance is None or hop < self.hop_distance):
            self.hop_distance = hop
        if self.status is not NodeStatus.DEFAULTED:
            self.status = NodeStatus.CONSTRAINED
        return first

    def mark_defaulted(self, t: float) -> bool:
        first = self.first_defaulted_t is None
        if first:
            self.first_defaulted_t = t
        self.status = NodeStatus.DEFAULTED
        return first

    def refresh_status(self) -> NodeStatus:
        """Recompute HEALTHY/STRESSED from the buffer, leaving worse states alone."""
        if self.status in (NodeStatus.CONSTRAINED, NodeStatus.DEFAULTED):
            return self.status
        warn_level = self.profile.stress_threshold_ratio * max(
            self.profile.opening_balance, 1.0
        )
        self.status = NodeStatus.STRESSED if self.buffer < warn_level else NodeStatus.HEALTHY
        return self.status

    def snapshot(
        self, t: float, pending_payable: float = 0.0, pending_receivable: float = 0.0,
        overdue_payable: float = 0.0,
    ) -> LiquidityState:
        return LiquidityState(
            merchant_id=self.merchant_id,
            t=t,
            cash_balance=self.cash,
            credit_drawn=self.credit_drawn,
            credit_limit=self.credit_limit,
            operating_floor=self.operating_floor,
            pending_payable=pending_payable,
            pending_receivable=pending_receivable,
            overdue_payable=overdue_payable,
            status=self.status,
        )
