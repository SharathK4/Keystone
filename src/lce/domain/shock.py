"""Liquidity shocks.

A shock is an exogenous perturbation to a node's cash process. Formally the
shock vector :math:`S(t) \\in \\mathbb{R}^n_{\\ge 0}` has components

.. math::

    S_i(t) = \\sum_{k \\in \\mathcal{K}_i} m_k \\, \\mathbb{1}[t = t_k]

for impulse shocks, and for windowed shocks (demand collapse) an additional
rate term :math:`\\dot S_i(t) = m_k / w_k` over :math:`[t_k, t_k + w_k)`.

The canonical demo shock is ``MISSED_INBOUND``: an expected receivable of
:math:`m` never arrives at merchant ``i``. That is *not* the same as draining
:math:`m` from the balance - it means a specific obligation owed **to** ``i`` is
written off, so ``i``'s creditor position is destroyed as well as its cash.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import Field, computed_field, model_validator

from lce.domain.base import DomainModel, MerchantId, ObligationId, new_id, utcnow
from lce.domain.enums import ShockKind


class ShockComponent(DomainModel):
    """One component :math:`(i, m_i, t_i)` of the shock vector."""

    merchant_id: MerchantId
    magnitude: float = Field(gt=0.0, description="m_i, INR withheld/withdrawn.")
    t: float = Field(ge=0.0, description="t_i, onset in simulation hours.")
    kind: ShockKind = ShockKind.MISSED_INBOUND

    duration_hours: float = Field(
        default=0.0,
        ge=0.0,
        description="w_i. 0 = impulse; >0 spreads the magnitude over a window.",
    )
    target_obligation_id: ObligationId | None = Field(
        default=None,
        description="For MISSED_INBOUND/COUNTERPARTY_DEFAULT: the receivable that fails.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_impulse(self) -> bool:
        return self.duration_hours <= 0.0

    @property
    def end_t(self) -> float:
        return self.t + self.duration_hours

    def rate(self) -> float:
        """INR/hour drain for windowed shocks; ``inf`` for impulses."""
        if self.is_impulse:
            return float("inf")
        return self.magnitude / self.duration_hours

    def magnitude_in(self, t0: float, t1: float) -> float:
        """Shock mass applied within ``[t0, t1)`` - the integral of S_i over the tick."""
        if self.is_impulse:
            return self.magnitude if t0 <= self.t < t1 else 0.0
        overlap = min(t1, self.end_t) - max(t0, self.t)
        if overlap <= 0:
            return 0.0
        return self.rate() * overlap


class Shock(DomainModel):
    """A named scenario: the full shock vector S applied to one network."""

    shock_id: str = Field(default_factory=lambda: new_id("shk"))
    name: str = ""
    description: str = ""
    components: list[ShockComponent] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        seen: set[tuple[str, float, str]] = set()
        for c in self.components:
            key = (c.merchant_id, c.t, str(c.kind))
            if key in seen:
                raise ValueError(
                    f"duplicate shock component for {c.merchant_id} at t={c.t} kind={c.kind}"
                )
            seen.add(key)
        return self

    @classmethod
    def single(
        cls,
        merchant_id: MerchantId,
        magnitude: float,
        t: float = 0.0,
        kind: ShockKind = ShockKind.MISSED_INBOUND,
        name: str = "",
        target_obligation_id: ObligationId | None = None,
    ) -> Shock:
        """The demo case: one merchant misses one expected payment."""
        return cls(
            name=name or f"single:{merchant_id}",
            components=[
                ShockComponent(
                    merchant_id=merchant_id,
                    magnitude=magnitude,
                    t=t,
                    kind=kind,
                    target_obligation_id=target_obligation_id,
                )
            ],
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_magnitude(self) -> float:
        """||S||_1, total INR of primary shock."""
        return sum(c.magnitude for c in self.components)

    @property
    def origin_ids(self) -> list[MerchantId]:
        return sorted({c.merchant_id for c in self.components})

    @property
    def onset_t(self) -> float:
        return min(c.t for c in self.components)

    def components_for(self, merchant_id: MerchantId) -> list[ShockComponent]:
        return [c for c in self.components if c.merchant_id == merchant_id]

    def magnitude_in(self, merchant_id: MerchantId, t0: float, t1: float) -> float:
        """Total shock mass hitting ``merchant_id`` during ``[t0, t1)``."""
        return sum(c.magnitude_in(t0, t1) for c in self.components_for(merchant_id))
