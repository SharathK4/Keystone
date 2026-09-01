"""Edge feature estimation from raw event streams.

Everything here is *descriptive* statistics over the observed payments - amount
law, recurrence, inter-arrival regularity. The harder, generative quantities
(pass-through, conditional probability, excitation) are estimated in
:mod:`lce.models.dependency`.

Separating them matters: these features are cheap, unambiguous and computable
for any pair that ever transacted, and they are the input features the temporal
GNN consumes. The dependency parameters are model-based estimates with
uncertainty, and they are what the system is scored on.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from lce.domain.edges import EdgeFeatures, LagDistribution
from lce.domain.enums import RecurrencePattern
from lce.domain.events import Obligation, ObligationStatus, PaymentEvent

HOURS_PER_DAY = 24.0
HOURS_PER_WEEK = 168.0

# Candidate recurrence periods, in hours, with the tolerance band used to
# decide whether an observed median inter-arrival matches one of them.
_PERIOD_CANDIDATES: list[tuple[RecurrencePattern, float]] = [
    (RecurrencePattern.DAILY, HOURS_PER_DAY),
    (RecurrencePattern.WEEKLY, HOURS_PER_WEEK),
    (RecurrencePattern.BIWEEKLY, 2 * HOURS_PER_WEEK),
    (RecurrencePattern.MONTHLY, 30 * HOURS_PER_DAY),
    (RecurrencePattern.QUARTERLY, 91 * HOURS_PER_DAY),
]
_PERIOD_TOLERANCE = 0.30


def classify_recurrence(inter_arrivals: np.ndarray) -> tuple[RecurrencePattern, float | None]:
    """Match the median inter-arrival against known billing cadences."""
    if inter_arrivals.size == 0:
        return RecurrencePattern.ONE_OFF, None
    median = float(np.median(inter_arrivals))
    if median <= 0:
        return RecurrencePattern.IRREGULAR, None
    for pattern, period in _PERIOD_CANDIDATES:
        if abs(median - period) / period <= _PERIOD_TOLERANCE:
            return pattern, period
    return RecurrencePattern.IRREGULAR, median


def regularity_score(inter_arrivals: np.ndarray) -> float:
    """rho = max(0, 1 - CV) of inter-arrival times. 1.0 means clockwork."""
    if inter_arrivals.size < 2:
        return 0.0
    mean = float(inter_arrivals.mean())
    if mean <= 0:
        return 0.0
    cv = float(inter_arrivals.std(ddof=1)) / mean
    return float(np.clip(1.0 - cv, 0.0, 1.0))


def compute_edge_features(events: Sequence[PaymentEvent]) -> EdgeFeatures:
    """Summarise one directed edge's payment history."""
    if not events:
        return EdgeFeatures()

    times = np.array([e.t for e in events], dtype=float)
    amounts = np.array([e.amount for e in events], dtype=float)
    order = np.argsort(times)
    times, amounts = times[order], amounts[order]
    gaps = np.diff(times)

    pattern, period = classify_recurrence(gaps)
    return EdgeFeatures(
        n_events=len(events),
        total_amount=float(amounts.sum()),
        mean_amount=float(amounts.mean()),
        std_amount=float(amounts.std(ddof=1)) if amounts.size > 1 else 0.0,
        first_t=float(times[0]),
        last_t=float(times[-1]),
        recurrence=pattern,
        period_hours=period,
        regularity=regularity_score(gaps),
    )


def estimate_response_lags(
    inflow_times: np.ndarray,
    outflow_times: np.ndarray,
    *,
    max_lag_hours: float = 30 * HOURS_PER_DAY,
) -> np.ndarray:
    """Lags between each outflow and the most recent preceding inflow.

    This is the raw material for the edge's lag law: *how long after being paid
    does this merchant pay on?* Outflows with no preceding inflow inside the
    window are dropped rather than imputed - they are baseline payments, not
    responses, and folding them in would bias the lag law upward.
    """
    if inflow_times.size == 0 or outflow_times.size == 0:
        return np.empty(0, dtype=float)

    inflow_sorted = np.sort(inflow_times)
    # Index of the last inflow strictly before each outflow.
    idx = np.searchsorted(inflow_sorted, outflow_times, side="left") - 1
    valid = idx >= 0
    if not np.any(valid):
        return np.empty(0, dtype=float)

    lags = outflow_times[valid] - inflow_sorted[idx[valid]]
    return lags[(lags > 0) & (lags <= max_lag_hours)]


def fit_lag_distribution(
    inflow_times: np.ndarray,
    outflow_times: np.ndarray,
    *,
    floor_hours: float = 0.0,
    fallback_mean: float = 48.0,
) -> LagDistribution:
    """Fit the edge's log-normal lag law, falling back to a wide prior."""
    lags = estimate_response_lags(inflow_times, outflow_times)
    if lags.size < 2:
        return LagDistribution.from_mean_cv(fallback_mean, cv=1.0, floor_hours=floor_hours)
    return LagDistribution.from_samples(lags, floor_hours=floor_hours)


def estimate_reliability(
    settled: Sequence[Obligation] = (),
    *,
    features: EdgeFeatures | None = None,
) -> tuple[float, str]:
    """Estimate r_ij = P(settles on or before deadline).

    Returns ``(estimate, basis)``.

    With settled obligations available the estimate is the direct empirical
    on-time rate, Laplace-smoothed so a link with two observations is not
    reported as 0.0 or 1.0.

    Without them - which is the case when only a payment stream has been
    observed, as in the historical window - there is no deadline to compare
    against, so reliability is *not* identifiable. We fall back to inter-arrival
    regularity as a proxy and label it as such, rather than presenting a
    fabricated number as a measurement.
    """
    resolved = [
        o
        for o in settled
        if o.status
        in (
            ObligationStatus.SETTLED,
            ObligationStatus.SETTLED_LATE,
            ObligationStatus.DEFAULTED,
        )
    ]
    if resolved:
        on_time = sum(1 for o in resolved if o.status is ObligationStatus.SETTLED)
        return (on_time + 1.0) / (len(resolved) + 2.0), "empirical"

    if features is not None and features.n_events > 0:
        # Map regularity in [0, 1] onto a plausible reliability band. A link
        # that fires like clockwork is usually a link that pays on time.
        return float(0.55 + 0.40 * features.regularity), "regularity_proxy"

    return 0.75, "prior"
