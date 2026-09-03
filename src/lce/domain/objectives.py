"""The network disruption objective :math:`D(G, S)`.

Definition
----------
.. math::

    D(G, S) = \\sum_{i \\in V} w_i \\Big[
        \\gamma_1 \\!\\!\\sum_{o \\in O_i^{\\text{late}}}\\!\\! a_o \\, \\phi(\\delta_o)
        \\;+\\; \\gamma_2 \\, \\mathbb{1}[\\text{default}_i]
        \\;+\\; \\gamma_3 \\!\\int_0^T\\! \\big(\\underline{L}_i - L_i(t)\\big)^+ dt
    \\Big]

with

* :math:`w_i` - the merchant's systemic weight (payroll size, criticality, ...),
* :math:`\\phi(\\delta) = \\delta / \\text{delay unit}` - lateness in days,
* :math:`\\gamma_1, \\gamma_2, \\gamma_3` - configurable weights on
  *value-weighted delay*, *default count*, and *liquidity deficit-time*.

The three terms are deliberately different units, reconciled by the gammas:
term 1 is INR-days, term 2 is a count, term 3 is INR-hours. The defaults in
:class:`~lce.config.ObjectiveSettings` are calibrated so that a single default
dominates a few days of delay - which is the economically right ordering - but
every weight is an experiment parameter and is recorded in the run manifest.

Because :math:`D` is evaluated by *simulating*, ``D(G, S, U)`` for an
intervention set ``U`` is a black-box function. That is why the optimiser in
``lce.optimization`` is a search over counterfactual simulations rather than a
closed-form program.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lce.config import ObjectiveSettings

if TYPE_CHECKING:  # pragma: no cover
    from lce.domain.propagation import CascadeResult, NodeOutcome


@dataclass(frozen=True, slots=True)
class DisruptionBreakdown:
    """The objective decomposed by term, so results stay interpretable."""

    delay_term: float
    default_term: float
    deficit_term: float

    @property
    def total(self) -> float:
        return self.delay_term + self.default_term + self.deficit_term

    def to_dict(self) -> dict[str, float]:
        return {
            "delay_term": self.delay_term,
            "default_term": self.default_term,
            "deficit_term": self.deficit_term,
            "total": self.total,
        }


def phi(delay_hours: float, delay_unit_hours: float = 24.0) -> float:
    """Lateness penalty :math:`\\phi(\\delta) = \\delta^+ / \\text{unit}` (days late)."""
    return max(0.0, delay_hours) / delay_unit_hours


def discount(t: float, rate_per_hour: float) -> float:
    """Exponential discount :math:`e^{-rt}`. Identity when ``rate_per_hour`` is 0."""
    if rate_per_hour <= 0.0:
        return 1.0
    return math.exp(-rate_per_hour * max(0.0, t))


def node_disruption(
    outcome: NodeOutcome,
    settings: ObjectiveSettings | None = None,
) -> DisruptionBreakdown:
    """Per-node contribution
    :math:`w_i [\\gamma_1 \\ldots + \\gamma_2 \\ldots + \\gamma_3 \\ldots]`.

    ``outcome.weighted_delay`` already carries :math:`\\sum a_o \\phi(\\delta_o)`,
    accumulated by the simulator as obligations settle, so the objective does
    not need to re-walk the obligation book.
    """
    cfg = settings or ObjectiveSettings()
    w = outcome.systemic_weight
    delay = cfg.gamma_delay * outcome.weighted_delay
    default = cfg.gamma_default * outcome.defaults_caused
    deficit = cfg.gamma_deficit * outcome.deficit_integral
    return DisruptionBreakdown(
        delay_term=w * delay,
        default_term=w * default,
        deficit_term=w * deficit,
    )


def compute_disruption(
    result: CascadeResult,
    settings: ObjectiveSettings | None = None,
) -> DisruptionBreakdown:
    """:math:`D(G, S)` for a completed cascade run."""
    cfg = settings or ObjectiveSettings()
    delay = default = deficit = 0.0
    for outcome in result.outcomes.values():
        part = node_disruption(outcome, cfg)
        delay += part.delay_term
        default += part.default_term
        deficit += part.deficit_term
    return DisruptionBreakdown(delay_term=delay, default_term=default, deficit_term=deficit)


def disruption_prevented(baseline: float, residual: float) -> float:
    """:math:`D(G,S,\\emptyset) - D(G,S,U)`. Negative means the plan made things worse."""
    return baseline - residual


def disruption_prevented_per_rupee(baseline: float, residual: float, cost: float) -> float:
    """DPR - the metric the intervention ranking is sorted on."""
    prevented = disruption_prevented(baseline, residual)
    if cost <= 0.0:
        return float("inf") if prevented > 0 else 0.0
    return prevented / cost


def systemic_importance(
    per_shock_disruption: dict[str, float],
    normalise: bool = True,
) -> dict[str, float]:
    """Rank merchants by the damage a shock *at* them causes elsewhere.

    :math:`\\mathrm{SI}_i = \\mathbb{E}_{S \\sim \\mathcal{S}_i}[D(G, S)]`, estimated
    by simulating a standardised unit shock at each node. Normalised to
    ``[0, 1]`` against the worst node so the ranking is readable, which is the
    closing beat of the demo: which merchants are structurally load-bearing,
    independent of their own size.
    """
    if not per_shock_disruption:
        return {}
    if not normalise:
        return dict(per_shock_disruption)
    worst = max(per_shock_disruption.values())
    if worst <= 0:
        return dict.fromkeys(per_shock_disruption, 0.0)
    return {k: v / worst for k, v in per_shock_disruption.items()}
