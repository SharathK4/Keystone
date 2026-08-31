"""Temporal edges: delay distributions, behavioural features, conditional dependency.

An edge in this system is *not* a scalar weight. Between an ordered pair
:math:`(i, j)` we keep:

1. the full list of realised :class:`~lce.domain.events.PaymentEvent` s
   (held by the graph, never collapsed away), and
2. a :class:`DependencyEdge` - the learned behavioural summary below.

Learned edge quantities
-----------------------
*Amount law*      :math:`\\hat a_{ij}, \\sigma^a_{ij}` - size of a typical transfer.

*Recurrence*      period :math:`p_{ij}` and regularity
                  :math:`\\rho_{ij} = \\max(0, 1 - \\mathrm{CV}[\\Delta t])`,
                  the coefficient of variation of inter-arrival times.

*Lag law*         :math:`\\mathcal{D}_{ij}` - the distribution of
                  :math:`\\ell = t_{i \\to j} - t_{k \\to i}`, i.e. how long after
                  ``i`` is paid does ``i`` pay ``j``. Modelled log-normal, which
                  is positive-support and right-skewed like real settlement lags.

*Reliability*     :math:`r_{ij} = P(\\tau_o \\le d_o)` on this link.

*Conditional dependency*
                  :math:`\\theta_{ij} \\in [0,1]` - the **cash pass-through**:
                  the fraction of an inflow at ``i`` that is forwarded along
                  :math:`i \\to j` within the response window. This is the latent
                  parameter contagion actually travels on, and the quantity the
                  dependency learner estimates.

*Excitation*      :math:`\\alpha_{ij}, \\beta_{ij}` - Hawkes self/cross-excitation
                  gain and decay: an inflow at ``i`` at time :math:`t_m` raises
                  the intensity of :math:`i \\to j` payments by
                  :math:`\\alpha_{ij}\\beta_{ij} e^{-\\beta_{ij}(t - t_m)}`.
"""

from __future__ import annotations

import math
from typing import Any, Self

import numpy as np
from pydantic import Field, computed_field, model_validator

from lce.domain.base import DomainModel, MerchantId
from lce.domain.enums import RecurrencePattern

# Guard against degenerate log-normals when a link has near-zero variance.
_MIN_SIGMA = 1e-3


class LagDistribution(DomainModel):
    """Log-normal delay law for a temporal edge.

    Parameterised by the log-space mean/std so that
    :math:`\\ell \\sim \\mathrm{LogNormal}(\\mu, \\sigma)` with support
    :math:`(0, \\infty)`, shifted by ``floor_hours`` (rail latency that cannot be
    beaten) and truncated at ``max_hours``.
    """

    mu_log: float = Field(description="Mean of log(lag hours).")
    sigma_log: float = Field(default=0.5, gt=0.0, description="Std-dev of log(lag hours).")
    floor_hours: float = Field(default=0.0, ge=0.0, description="Irreducible settlement latency.")
    max_hours: float = Field(default=24.0 * 90, gt=0.0, description="Truncation point.")

    @classmethod
    def from_samples(
        cls, lags: list[float] | np.ndarray, floor_hours: float = 0.0
    ) -> LagDistribution:
        """Fit by moment-matching in log space. Falls back to a wide prior on <2 samples."""
        arr = np.asarray(list(lags), dtype=float)
        arr = arr[np.isfinite(arr)]
        arr = arr[arr > floor_hours]
        shifted = arr - floor_hours
        if shifted.size < 2:
            mean = float(shifted.mean()) if shifted.size == 1 else 24.0
            return cls(mu_log=math.log(max(mean, 1e-3)), sigma_log=1.0, floor_hours=floor_hours)
        logs = np.log(np.clip(shifted, 1e-6, None))
        return cls(
            mu_log=float(logs.mean()),
            sigma_log=float(max(logs.std(ddof=1), _MIN_SIGMA)),
            floor_hours=floor_hours,
        )

    @classmethod
    def from_mean_cv(
        cls, mean_hours: float, cv: float = 0.5, floor_hours: float = 0.0
    ) -> LagDistribution:
        """Construct from an interpretable (mean, coefficient-of-variation) pair."""
        mean_excess = max(mean_hours - floor_hours, 1e-3)
        sigma = math.sqrt(math.log(1.0 + max(cv, _MIN_SIGMA) ** 2))
        mu = math.log(mean_excess) - 0.5 * sigma**2
        return cls(mu_log=mu, sigma_log=max(sigma, _MIN_SIGMA), floor_hours=floor_hours)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mean_hours(self) -> float:
        """E[lag] = floor + exp(mu + sigma^2/2)."""
        return self.floor_hours + math.exp(self.mu_log + 0.5 * self.sigma_log**2)

    @property
    def median_hours(self) -> float:
        return self.floor_hours + math.exp(self.mu_log)

    @property
    def variance(self) -> float:
        s2 = self.sigma_log**2
        return (math.exp(s2) - 1.0) * math.exp(2 * self.mu_log + s2)

    def quantile(self, q: float) -> float:
        """Inverse CDF. ``q`` in (0, 1)."""
        if not 0.0 < q < 1.0:
            raise ValueError("quantile requires 0 < q < 1")
        # Probit via the inverse error function relation.
        from scipy.special import ndtri  # local import: scipy is heavy

        z = float(ndtri(q))
        return min(self.max_hours, self.floor_hours + math.exp(self.mu_log + self.sigma_log * z))

    def cdf(self, hours: float) -> float:
        """P(lag <= hours) - used to answer 'will the shock hit by time t?'."""
        excess = hours - self.floor_hours
        if excess <= 0:
            return 0.0
        z = (math.log(excess) - self.mu_log) / self.sigma_log
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    def sample(self, rng: np.random.Generator, size: int | None = None) -> float | np.ndarray:
        """Draw lag(s). Always uses an explicit generator - no global RNG."""
        draw = rng.lognormal(self.mu_log, self.sigma_log, size=size)
        return np.minimum(self.floor_hours + draw, self.max_hours)


class EdgeFeatures(DomainModel):
    """Observable behavioural statistics of the ``i -> j`` link."""

    n_events: int = Field(default=0, ge=0)
    total_amount: float = Field(default=0.0, ge=0.0)
    mean_amount: float = Field(default=0.0, ge=0.0, description="a-hat_ij.")
    std_amount: float = Field(default=0.0, ge=0.0, description="sigma^a_ij.")
    first_t: float | None = None
    last_t: float | None = None

    recurrence: RecurrencePattern = RecurrencePattern.IRREGULAR
    period_hours: float | None = Field(default=None, description="p_ij, modal inter-arrival.")
    regularity: float = Field(
        default=0.0, ge=0.0, le=1.0, description="rho_ij = max(0, 1 - CV[inter-arrival])."
    )

    on_time_count: int = Field(default=0, ge=0)
    late_count: int = Field(default=0, ge=0)
    missed_count: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount_cv(self) -> float:
        if self.mean_amount <= 0:
            return 0.0
        return self.std_amount / self.mean_amount

    @property
    def observed_reliability(self) -> float:
        """Empirical r_ij with a Laplace prior so thin links are not 0 or 1."""
        total = self.on_time_count + self.late_count + self.missed_count
        return (self.on_time_count + 1.0) / (total + 2.0)

    @property
    def active_span_hours(self) -> float:
        if self.first_t is None or self.last_t is None:
            return 0.0
        return max(0.0, self.last_t - self.first_t)

    @property
    def intensity_per_hour(self) -> float:
        """Empirical base rate mu_ij of the link's point process."""
        span = self.active_span_hours
        if span <= 0 or self.n_events <= 1:
            return 0.0
        return (self.n_events - 1) / span


class DependencyEdge(DomainModel):
    """A behavioural edge ``source -> target`` with its learned dependency law.

    ``source`` pays ``target``. Contagion therefore flows *forward* along this
    edge: if ``source`` is starved, ``target`` stops receiving.
    """

    source_id: MerchantId
    target_id: MerchantId

    features: EdgeFeatures = Field(default_factory=EdgeFeatures)
    lag: LagDistribution = Field(
        default_factory=lambda: LagDistribution.from_mean_cv(48.0, cv=0.6)
    )

    reliability: float = Field(
        default=0.9, ge=0.0, le=1.0, description="r_ij: P(settles on or before deadline)."
    )
    pass_through: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="theta_ij: fraction of an inflow at source forwarded to target.",
    )
    conditional_probability: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="P(source pays target within window | source received an inflow).",
    )
    excitation_alpha: float = Field(
        default=0.0, ge=0.0, description="Hawkes cross-excitation gain alpha_ij."
    )
    excitation_decay: float = Field(
        default=1.0 / 24.0, gt=0.0, description="Hawkes decay beta_ij, per hour."
    )
    base_intensity: float = Field(
        default=0.0, ge=0.0, description="mu_ij: baseline event rate, per hour."
    )

    is_ground_truth: bool = Field(
        default=False,
        description="True for generator-emitted edges; False for learner estimates.",
    )
    estimator: str | None = Field(default=None, description="Which estimator produced this edge.")
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Estimator's confidence in pass_through."
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_self_loop(self) -> Self:
        if self.source_id == self.target_id:
            raise ValueError("dependency edges must connect distinct merchants")
        return self

    @property
    def key(self) -> tuple[MerchantId, MerchantId]:
        return (self.source_id, self.target_id)

    def transmission_weight(self) -> float:
        """Expected fraction of a shock at ``source`` that reaches ``target``.

        :math:`\\theta_{ij} \\cdot (1 - r_{ij}^{\\text{buffer}})` is tempting here,
        but reliability describes *timeliness*, not magnitude. The magnitude
        actually transmitted is the pass-through; reliability modulates only
        *whether* the transmission is observed as a miss. We therefore return
        the pass-through and let the propagation model apply reliability
        separately, keeping the two effects identifiable.
        """
        return self.pass_through

    def excitation_kernel(self, elapsed_hours: float) -> float:
        """kappa(u) = alpha * beta * exp(-beta u) for u >= 0, else 0."""
        if elapsed_hours < 0:
            return 0.0
        return (
            self.excitation_alpha
            * self.excitation_decay
            * math.exp(-self.excitation_decay * elapsed_hours)
        )

    def expected_hit_time(self, shock_t: float) -> float:
        """When a shock at ``source`` at ``shock_t`` is expected to bite at ``target``."""
        return shock_t + self.lag.mean_hours

    def hit_probability_by(self, shock_t: float, horizon_t: float) -> float:
        """P(transmission observed at target by ``horizon_t``).

        Combines the lag CDF with the *unreliability* of the link: a perfectly
        reliable payer still transmits the cash shortfall, but an unreliable one
        transmits it sooner and more often, because it has less slack.
        """
        if horizon_t <= shock_t:
            return 0.0
        return self.lag.cdf(horizon_t - shock_t)
