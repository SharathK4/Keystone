"""Scoring primitives.

Implemented directly on NumPy rather than pulled from scikit-learn: the metric
definitions are part of the claim this project makes, so they should be readable
and auditable in-repo rather than delegated.

The ranking metrics use the standard definitions:

* **Average precision** (reported as ``pr_auc``) - the step-wise area under the
  precision/recall curve, :math:`\\sum_k (R_k - R_{k-1}) P_k`. Preferred over a
  trapezoidal PR area, which is optimistically biased.
* **ROC AUC** - computed via the Mann-Whitney U identity, with ties given half
  credit, so it is exact rather than a curve approximation.

Both are threshold-free, which matters because a predictor's threshold is a
deployment choice; the underlying ranking quality is the model property.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import numpy as np

from lce.domain.evaluation import ClassificationMetrics, InterventionMetrics, TimingMetrics


def confusion(
    predicted: Iterable[str], truth: Iterable[str], universe: Iterable[str]
) -> tuple[int, int, int, int]:
    """(tp, fp, fn, tn) over an explicit universe of candidates.

    The universe is required rather than inferred: true negatives are only
    meaningful relative to the set of nodes that *could* have been flagged, and
    silently inferring it from the union of the other two sets would make every
    true negative disappear.
    """
    all_nodes = set(universe)
    pred = set(predicted) & all_nodes
    real = set(truth) & all_nodes
    tp = len(pred & real)
    fp = len(pred - real)
    fn = len(real - pred)
    tn = len(all_nodes) - tp - fp - fn
    return tp, fp, fn, tn


def average_precision(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """Step-wise area under the precision/recall curve."""
    if scores.size == 0 or labels.sum() == 0:
        return None
    order = np.argsort(-scores, kind="stable")
    y = labels[order].astype(float)
    tp = np.cumsum(y)
    precision = tp / np.arange(1, y.size + 1)
    total_positive = float(labels.sum())
    # Only the steps where recall actually increases contribute.
    return float(np.sum(precision * y) / total_positive)


def roc_auc(scores: np.ndarray, labels: np.ndarray) -> float | None:
    """ROC AUC via the rank-sum identity, with ties at half credit."""
    positives = labels.astype(bool)
    n_pos = int(positives.sum())
    n_neg = int(labels.size - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = _average_ranks(scores)
    rank_sum = float(ranks[positives].sum())
    return float((rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def _average_ranks(values: np.ndarray) -> np.ndarray:
    """1-based ranks with ties averaged."""
    order = np.argsort(values, kind="stable")
    ranks = np.empty(values.size, dtype=float)
    ranks[order] = np.arange(1, values.size + 1, dtype=float)
    sorted_values = values[order]
    start = 0
    for i in range(1, values.size + 1):
        if i == values.size or sorted_values[i] != sorted_values[start]:
            if i - start > 1:
                ranks[order[start:i]] = ranks[order[start:i]].mean()
            start = i
    return ranks


def classification_metrics(
    scores: Mapping[str, float],
    truth: Iterable[str],
    *,
    universe: Iterable[str] | None = None,
    threshold: float = 0.5,
) -> ClassificationMetrics:
    """Precision/recall/F1 at a threshold, plus threshold-free PR-AUC and ROC-AUC."""
    nodes = sorted(universe) if universe is not None else sorted(scores)
    truth_set = set(truth)
    predicted = [n for n in nodes if scores.get(n, 0.0) >= threshold]
    tp, fp, fn, tn = confusion(predicted, truth_set, nodes)

    score_array = np.array([scores.get(n, 0.0) for n in nodes], dtype=float)
    label_array = np.array([1 if n in truth_set else 0 for n in nodes], dtype=int)

    return ClassificationMetrics(
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        pr_auc=average_precision(score_array, label_array),
        roc_auc=roc_auc(score_array, label_array),
        threshold=threshold,
    )


def timing_metrics(
    predicted: Mapping[str, float],
    truth: Mapping[str, float],
    *,
    restrict_to: Iterable[str] | None = None,
) -> TimingMetrics:
    """Error on the predicted time-to-impact, in hours.

    Scored only over nodes where both a prediction and a ground-truth hit time
    exist. Imputing a time for nodes the model never flagged would conflate
    detection failure (already measured by recall) with timing error.
    """
    keys = set(predicted) & set(truth)
    if restrict_to is not None:
        keys &= set(restrict_to)
    if not keys:
        return TimingMetrics(n_compared=0)

    ordered = sorted(keys)
    pred = np.array([predicted[k] for k in ordered], dtype=float)
    real = np.array([truth[k] for k in ordered], dtype=float)
    error = pred - real
    absolute = np.abs(error)

    return TimingMetrics(
        mae_hours=float(absolute.mean()),
        rmse_hours=float(np.sqrt(np.mean(error**2))),
        median_abs_error_hours=float(np.median(absolute)),
        bias_hours=float(error.mean()),
        n_compared=len(ordered),
        within_6h=float(np.mean(absolute <= 6.0)),
        within_24h=float(np.mean(absolute <= 24.0)),
    )


def intervention_metrics(
    *,
    baseline_disruption: float,
    achieved_disruption: float,
    cost: float,
    n_actions: int,
    optimal_disruption: float | None = None,
    optimal_cost: float | None = None,
    search_ms: float | None = None,
    candidates_considered: int = 0,
    simulations_run: int = 0,
) -> InterventionMetrics:
    """Package an optimiser's result, including the gap to the true optimum."""
    return InterventionMetrics(
        baseline_disruption=baseline_disruption,
        achieved_disruption=achieved_disruption,
        optimal_disruption=optimal_disruption,
        cost=cost,
        optimal_cost=optimal_cost,
        n_actions=n_actions,
        search_ms=search_ms,
        candidates_considered=candidates_considered,
        simulations_run=simulations_run,
    )


def attributable_affected(
    shocked_affected: Sequence[str], baseline_affected: Sequence[str]
) -> list[str]:
    """Contagion ground truth: nodes that fail *because of* the shock.

    The undisturbed network already has some habitual lateness and the odd
    stressed merchant. Scoring a contagion predictor against the raw affected
    set would credit it for those, inflating recall without measuring anything.
    Differencing against the no-shock baseline isolates the shock's causal
    contribution, which is what the model actually claims to predict.
    """
    return sorted(set(shocked_affected) - set(baseline_affected))
