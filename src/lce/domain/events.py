"""Payment events and obligations - the event-level substrate.

The system never reduces history to a static adjacency matrix. Everything is
derived from two event-level records:

**PaymentEvent** - a realised cash movement :math:`e = (i \\to j, a_e, t_e)`,
one arrival of the marked point process :math:`B_i` (outbound for ``i``) and
:math:`A_j` (inbound for ``j``).

**Obligation** - a *commitment* :math:`o = (i \\to j, a_o, d_o)`: merchant ``i``
owes ``j`` the amount :math:`a_o`, due at deadline :math:`d_o`. Settlement time
is :math:`\\tau_o`; the realised delay is :math:`\\delta_o = (\\tau_o - d_o)^+`.
Obligations are what contagion actually propagates along: a node that cannot
settle its obligations starves the counterparties that were counting on them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import Field, computed_field, model_validator

from lce.domain.base import (
    AMOUNT_TOL,
    DomainModel,
    EventId,
    MerchantId,
    ObligationId,
    new_id,
    utcnow,
)
from lce.domain.enums import (
    ObligationKind,
    ObligationStatus,
    PaymentChannel,
    PaymentStatus,
)

# Sink counterparty used for obligations that leave the modelled network
# (payroll, tax, rent). Cash still departs the node but no downstream merchant
# is starved by a miss, so these are excluded from contagion edges.
EXTERNAL_SINK: MerchantId = "external_sink"


class PaymentEvent(DomainModel):
    """A realised payment from ``payer_id`` to ``payee_id`` at time ``t``.

    This is the atomic observation the dependency learner consumes. It is kept
    at full resolution - amount, timestamp, channel, and the obligation it
    settles (when known) - because the latent conditional dependencies we want
    to learn are visible only in the *sequence* of these events, not in their
    aggregate.
    """

    event_id: EventId = Field(default_factory=lambda: new_id("pay"))
    payer_id: MerchantId
    payee_id: MerchantId
    amount: float = Field(gt=0.0, description="a_e, INR.")
    t: float = Field(description="t_e, simulation hours.")

    obligation_id: ObligationId | None = Field(
        default=None, description="Obligation this payment settles, when attributable."
    )
    channel: PaymentChannel = PaymentChannel.NEFT
    status: PaymentStatus = PaymentStatus.CAPTURED
    settlement_lag_hours: float = Field(
        default=0.0,
        ge=0.0,
        description="Rail latency between capture and funds availability at the payee.",
    )

    external_id: str | None = Field(default=None, description="Razorpay payment id, if sourced.")
    is_synthetic: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_self_payment(self) -> Self:
        if self.payer_id == self.payee_id:
            raise ValueError("a merchant cannot pay itself")
        return self

    @property
    def availability_time(self) -> float:
        """When the payee can actually spend the money: ``t_e + settlement lag``."""
        return self.t + self.settlement_lag_hours

    @property
    def edge_key(self) -> tuple[MerchantId, MerchantId]:
        return (self.payer_id, self.payee_id)

    @property
    def affects_network(self) -> bool:
        """False for flows to/from the external sink."""
        return EXTERNAL_SINK not in (self.payer_id, self.payee_id)


class Obligation(DomainModel):
    """A payment commitment ``debtor -> creditor`` of ``amount`` due at ``due_t``.

    Invariants
    ----------
    * ``amount_paid <= amount + AMOUNT_TOL``
    * ``status`` is derived from ``amount_paid``, ``settled_t`` and ``due_t``
      via :meth:`resolve_status` - never set inconsistently by callers.
    """

    obligation_id: ObligationId = Field(default_factory=lambda: new_id("obl"))
    debtor_id: MerchantId
    creditor_id: MerchantId
    amount: float = Field(gt=0.0, description="a_o, INR.")
    issued_t: float = Field(description="When the obligation came into existence.")
    due_t: float = Field(description="d_o, the payment deadline, in simulation hours.")

    kind: ObligationKind = ObligationKind.TRADE_PAYABLE
    status: ObligationStatus = ObligationStatus.PENDING
    amount_paid: float = Field(default=0.0, ge=0.0)
    settled_t: float | None = Field(default=None, description="tau_o, settlement time.")

    # Term structure. `original_due_t` survives term-extension interventions so
    # the true (pre-intervention) deadline is always recoverable.
    original_due_t: float | None = None
    parent_obligation_id: ObligationId | None = Field(
        default=None, description="Set on tranches produced by a restructure."
    )
    priority: int = Field(
        default=0,
        description="Higher is paid first when cash is short (e.g. loan repayments before trade).",
    )

    external_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.debtor_id == self.creditor_id:
            raise ValueError("an obligation cannot have the same debtor and creditor")
        if self.due_t < self.issued_t:
            raise ValueError("due_t precedes issued_t")
        if self.amount_paid > self.amount + AMOUNT_TOL:
            raise ValueError("amount_paid exceeds obligation amount")
        return self

    # --- derived quantities ------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def outstanding(self) -> float:
        """Remaining principal a_o - paid."""
        return max(0.0, self.amount - self.amount_paid)

    @property
    def is_open(self) -> bool:
        return self.status in (ObligationStatus.PENDING, ObligationStatus.PARTIALLY_SETTLED)

    @property
    def is_network_edge(self) -> bool:
        """Whether a miss on this obligation starves a modelled merchant."""
        return EXTERNAL_SINK not in (self.debtor_id, self.creditor_id)

    @property
    def effective_due_t(self) -> float:
        return self.due_t

    def delay(self) -> float:
        """delta_o = (tau_o - d_o)^+, in hours. 0 while unsettled."""
        if self.settled_t is None:
            return 0.0
        return max(0.0, self.settled_t - self.due_t)

    def is_overdue_at(self, t: float) -> bool:
        return self.is_open and t > self.due_t

    def is_defaulted_at(self, t: float, grace_hours: float) -> bool:
        return self.is_open and t > self.due_t + grace_hours

    def shortfall_at(self, t: float) -> float:
        """Unpaid amount that is already past its deadline at ``t``."""
        return self.outstanding if self.is_overdue_at(t) else 0.0

    # --- state transitions (return new instances; domain objects are frozen) --

    def resolve_status(self, t: float, grace_hours: float) -> ObligationStatus:
        """Status implied by the current payment facts. Single source of truth."""
        if self.status in (ObligationStatus.CANCELLED, ObligationStatus.RESTRUCTURED):
            return self.status
        if self.outstanding <= AMOUNT_TOL:
            settled = self.settled_t if self.settled_t is not None else t
            return (
                ObligationStatus.SETTLED_LATE
                if settled > self.due_t
                else ObligationStatus.SETTLED
            )
        if t > self.due_t + grace_hours:
            return ObligationStatus.DEFAULTED
        if self.amount_paid > AMOUNT_TOL:
            return ObligationStatus.PARTIALLY_SETTLED
        return ObligationStatus.PENDING

    def with_payment(self, amount: float, t: float, grace_hours: float) -> Obligation:
        """Return a copy with ``amount`` applied at time ``t``."""
        if amount <= 0:
            raise ValueError("payment amount must be positive")
        paid = min(self.amount, self.amount_paid + amount)
        settled_t = t if paid >= self.amount - AMOUNT_TOL else self.settled_t
        draft = self.model_copy(update={"amount_paid": paid, "settled_t": settled_t})
        return draft.model_copy(update={"status": draft.resolve_status(t, grace_hours)})

    def with_deadline(self, new_due_t: float) -> Obligation:
        """Return a copy with an extended/accelerated deadline, preserving the original."""
        return self.model_copy(
            update={
                "due_t": new_due_t,
                "original_due_t": self.original_due_t
                if self.original_due_t is not None
                else self.due_t,
            }
        )

    def touched(self, t: float, grace_hours: float) -> Obligation:
        """Recompute status at time ``t`` without changing payment facts."""
        return self.model_copy(update={"status": self.resolve_status(t, grace_hours)})
