"""Shock scenario construction.

Turning a human description ("merchant X misses its biggest expected payment")
into a well-formed :class:`~lce.domain.shock.Shock` against a concrete graph.
"""

from __future__ import annotations

import numpy as np

from lce.domain.enums import ShockKind
from lce.domain.shock import Shock, ShockComponent
from lce.errors import ValidationError
from lce.graph.temporal_graph import TemporalPaymentGraph


def missed_receivable_shock(
    graph: TemporalPaymentGraph,
    merchant_id: str,
    *,
    t: float = 0.0,
    magnitude: float | None = None,
    name: str = "",
) -> Shock:
    """Merchant ``merchant_id`` does not receive an expected payment.

    Picks the merchant's largest open receivable due at or after ``t`` and
    writes it off. When ``magnitude`` is given, a bare cash shortfall of that
    size is applied instead, which is the right model when the missing inflow
    is not represented by a tracked obligation.
    """
    if not graph.has_merchant(merchant_id):
        raise ValidationError(f"unknown merchant {merchant_id!r}", merchant_id=merchant_id)

    if magnitude is not None:
        return Shock.single(
            merchant_id,
            magnitude=magnitude,
            t=t,
            kind=ShockKind.MISSED_INBOUND,
            name=name or f"missed_inbound:{merchant_id}",
        )

    candidates = [o for o in graph.receivables_of(merchant_id) if o.due_t >= t and o.is_open]
    if not candidates:
        raise ValidationError(
            f"merchant {merchant_id!r} has no open receivable due at or after t={t}; "
            "pass an explicit magnitude instead",
            merchant_id=merchant_id,
        )
    target = max(candidates, key=lambda o: (o.outstanding, -o.due_t, o.obligation_id))
    return Shock(
        name=name or f"missed_receivable:{merchant_id}",
        description=(
            f"{merchant_id} does not receive INR {target.outstanding:,.0f} "
            f"from {target.debtor_id} (due t={target.due_t:.0f}h)"
        ),
        components=[
            ShockComponent(
                merchant_id=merchant_id,
                magnitude=max(target.outstanding, 1e-6),
                t=t,
                kind=ShockKind.MISSED_INBOUND,
                target_obligation_id=target.obligation_id,
            )
        ],
    )


def unit_shock(
    graph: TemporalPaymentGraph,
    merchant_id: str,
    *,
    fraction_of_buffer: float = 1.0,
    t: float = 0.0,
) -> Shock:
    """A standardised shock for systemic-importance sweeps.

    Every node is hit with the same *relative* severity - a fixed fraction of
    its own opening buffer - so the resulting ranking reflects the node's
    position in the network rather than its size.
    """
    profile = graph.merchant(merchant_id)
    magnitude = max(profile.initial_buffer * fraction_of_buffer, 1.0)
    return Shock.single(
        merchant_id,
        magnitude=magnitude,
        t=t,
        kind=ShockKind.CASH_WITHDRAWAL,
        name=f"unit_shock:{merchant_id}",
    )


def multi_node_shock(
    graph: TemporalPaymentGraph,
    merchant_ids: list[str],
    magnitude: float,
    *,
    t: float = 0.0,
    name: str = "",
) -> Shock:
    """Simultaneous shock across several nodes (a sector-wide event)."""
    unknown = [m for m in merchant_ids if not graph.has_merchant(m)]
    if unknown:
        raise ValidationError(f"unknown merchants: {unknown}")
    return Shock(
        name=name or f"multi:{len(merchant_ids)}",
        components=[
            ShockComponent(
                merchant_id=m, magnitude=magnitude, t=t, kind=ShockKind.CASH_WITHDRAWAL
            )
            for m in merchant_ids
        ],
    )


def demand_collapse_shock(
    graph: TemporalPaymentGraph,
    merchant_id: str,
    *,
    magnitude: float,
    duration_hours: float,
    t: float = 0.0,
) -> Shock:
    """A sustained revenue drop spread over a window rather than an impulse."""
    if not graph.has_merchant(merchant_id):
        raise ValidationError(f"unknown merchant {merchant_id!r}")
    if duration_hours <= 0:
        raise ValidationError("duration_hours must be positive for a demand collapse")
    return Shock(
        name=f"demand_collapse:{merchant_id}",
        components=[
            ShockComponent(
                merchant_id=merchant_id,
                magnitude=magnitude,
                t=t,
                kind=ShockKind.DEMAND_COLLAPSE,
                duration_hours=duration_hours,
            )
        ],
    )


def random_shock(
    graph: TemporalPaymentGraph,
    rng: np.random.Generator,
    *,
    fraction_of_buffer: float = 0.8,
    t: float = 0.0,
) -> Shock:
    """Uniformly-random single-node shock, for evaluation sweeps."""
    ids = graph.merchant_ids
    if not ids:
        raise ValidationError("cannot draw a shock from an empty graph")
    chosen = str(rng.choice(sorted(ids)))
    return unit_shock(graph, chosen, fraction_of_buffer=fraction_of_buffer, t=t)
