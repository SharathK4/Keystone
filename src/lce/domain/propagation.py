"""Propagation events and cascade outcomes.

The simulator is an event recorder, not a scalar function: every state change
is emitted as a :class:`PropagationEvent` carrying its causal parent. That makes
a cascade auditable - for any downstream default you can walk back through the
chain of missed obligations to the originating shock.

Key derived objects
-------------------
:class:`NodeOutcome`    per-merchant summary: did it become constrained, when,
                        how deep was the deficit, how much value did it delay.
:class:`CascadeResult`  the whole run: events, outcomes, and the affected set
                        :math:`\\mathcal{A}(G, S)` used as ground truth when
                        scoring contagion predictions.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import Field, computed_field, model_validator

from lce.domain.base import DomainModel, EventId, MerchantId, ObligationId, new_id
from lce.domain.enums import NodeStatus, PropagationEventType


class PropagationEvent(DomainModel):
    """One discrete state change during a cascade."""

    event_id: EventId = Field(default_factory=lambda: new_id("prp"))
    sequence: int = Field(default=0, ge=0, description="Monotone order within the run.")
    t: float = Field(description="Simulation time in hours.")
    type: PropagationEventType
    merchant_id: MerchantId

    counterparty_id: MerchantId | None = None
    obligation_id: ObligationId | None = None
    amount: float = Field(default=0.0, description="INR moved / missed / injected.")

    # Causality: the event that caused this one. The root shock has None.
    caused_by: EventId | None = None
    hop: int = Field(
        default=0, ge=0, description="Cascade depth: 0 = directly shocked, 1 = first ring, ..."
    )

    balance_after: float | None = None
    buffer_after: float | None = None
    status_after: NodeStatus | None = None
    detail: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_impact(self) -> bool:
        """Whether this event marks real damage (vs. bookkeeping)."""
        return self.type in _IMPACT_TYPES


_IMPACT_TYPES = frozenset(
    {
        PropagationEventType.PAYMENT_MISSED,
        PropagationEventType.PAYMENT_PARTIAL,
        PropagationEventType.PAYMENT_DELAYED,
        PropagationEventType.NODE_CONSTRAINED,
        PropagationEventType.NODE_DEFAULTED,
    }
)


class NodeOutcome(DomainModel):
    """Per-merchant result of a cascade run."""

    merchant_id: MerchantId
    systemic_weight: float = Field(default=1.0, ge=0.0, description="w_i.")

    final_status: NodeStatus = NodeStatus.HEALTHY
    was_shocked: bool = Field(default=False, description="Received a component of S directly.")
    became_constrained: bool = False
    became_defaulted: bool = False

    first_constrained_t: float | None = Field(
        default=None, description="First t with C_i(t) = 1. The 'time to impact'."
    )
    first_defaulted_t: float | None = None
    hop_distance: int | None = Field(
        default=None, description="Cascade hops from the nearest shock origin. None if unaffected."
    )

    # Damage components feeding D(G, S).
    value_delayed: float = Field(default=0.0, ge=0.0, description="Sum a_o over late obligations.")
    weighted_delay: float = Field(
        default=0.0, ge=0.0, description="Sum a_o * phi(delta_o) over this node's payables."
    )
    defaults_caused: int = Field(default=0, ge=0)
    deficit_integral: float = Field(
        default=0.0,
        ge=0.0,
        description="Integral of (L-underbar_i - L_i(t))^+ dt, INR-hours below the floor.",
    )
    min_buffer: float = Field(default=0.0, description="min_t b_i(t) over the horizon.")
    final_balance: float = 0.0

    obligations_missed: int = Field(default=0, ge=0)
    obligations_settled_late: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_affected(self) -> bool:
        """Membership test for the affected set A(G, S).

        A node is *affected* if it was unable to meet an obligation: it became
        constrained, defaulted, or missed a deadline it had the intent but not
        the cash to meet.

        Settling late by *choice* deliberately does not count. Payers in this
        model are habitually somewhat late (that is what makes edge reliability
        learnable at all), and folding that into the affected set would swamp
        the ground truth with nodes that were never short of money - inflating
        recall while measuring nothing.
        """
        return (
            self.became_constrained
            or self.became_defaulted
            or self.obligations_missed > 0
        )

    @property
    def is_downstream_affected(self) -> bool:
        """Affected *without* having been shocked directly - true contagion."""
        return self.is_affected and not self.was_shocked


class CascadeResult(DomainModel):
    """Complete outcome of simulating shock ``S`` on network ``G``."""

    run_id: str = Field(default_factory=lambda: new_id("run"))
    shock_id: str | None = None
    plan_id: str | None = Field(default=None, description="Intervention plan applied, if any.")

    horizon_hours: float = Field(gt=0.0)
    events: list[PropagationEvent] = Field(default_factory=list)
    outcomes: dict[MerchantId, NodeOutcome] = Field(default_factory=dict)

    # Objective value, computed by lce.domain.objectives.compute_disruption.
    disruption: float | None = None
    disruption_breakdown: dict[str, float] = Field(default_factory=dict)

    seed: int | None = None
    config_hash: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sorted_events(self) -> Self:
        seqs = [e.sequence for e in self.events]
        if seqs != sorted(seqs):
            raise ValueError("propagation events must be in non-decreasing sequence order")
        return self

    # --- affected sets ------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def affected_ids(self) -> list[MerchantId]:
        """A(G, S): every merchant that broke a commitment."""
        return sorted(m for m, o in self.outcomes.items() if o.is_affected)

    @property
    def downstream_affected_ids(self) -> list[MerchantId]:
        """A(G, S) minus the directly shocked nodes."""
        return sorted(m for m, o in self.outcomes.items() if o.is_downstream_affected)

    @property
    def defaulted_ids(self) -> list[MerchantId]:
        return sorted(m for m, o in self.outcomes.items() if o.became_defaulted)

    @property
    def shocked_ids(self) -> list[MerchantId]:
        return sorted(m for m, o in self.outcomes.items() if o.was_shocked)

    def affected_by(self, t: float) -> list[MerchantId]:
        """Affected set restricted to nodes hit on or before ``t`` - the 6h/24h/48h view."""
        return sorted(
            m
            for m, o in self.outcomes.items()
            if o.first_constrained_t is not None and o.first_constrained_t <= t
        )

    def hit_times(self) -> dict[MerchantId, float]:
        """Ground-truth time-to-impact per affected node."""
        return {
            m: o.first_constrained_t
            for m, o in self.outcomes.items()
            if o.first_constrained_t is not None
        }

    def max_hop(self) -> int:
        hops = [o.hop_distance for o in self.outcomes.values() if o.hop_distance is not None]
        return max(hops) if hops else 0

    def events_for(self, merchant_id: MerchantId) -> list[PropagationEvent]:
        return [e for e in self.events if e.merchant_id == merchant_id]

    def causal_chain(self, event_id: EventId) -> list[PropagationEvent]:
        """Walk ``caused_by`` back to the root shock. Ordered root-first."""
        index = {e.event_id: e for e in self.events}
        chain: list[PropagationEvent] = []
        seen: set[str] = set()
        cursor: str | None = event_id
        while cursor is not None and cursor in index and cursor not in seen:
            seen.add(cursor)
            event = index[cursor]
            chain.append(event)
            cursor = event.caused_by
        return list(reversed(chain))

    def summary(self) -> dict[str, Any]:
        """Compact headline numbers for the API and the demo narrative."""
        return {
            "run_id": self.run_id,
            "horizon_hours": self.horizon_hours,
            "n_merchants": len(self.outcomes),
            "n_affected": len(self.affected_ids),
            "n_downstream_affected": len(self.downstream_affected_ids),
            "n_defaulted": len(self.defaulted_ids),
            "max_hop": self.max_hop(),
            "total_value_delayed": sum(o.value_delayed for o in self.outcomes.values()),
            "n_events": len(self.events),
            "disruption": self.disruption,
            "disruption_breakdown": self.disruption_breakdown,
        }
