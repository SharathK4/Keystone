"""Scoring Phase-3 models: discrimination, timing, and calibration together.

Three families of number, reported side by side because each is blind to what
the others measure:

**Discrimination** - PR-AUC and ROC-AUC. Threshold-free, so they measure the
ranking rather than a deployment choice. PR-AUC is the one to read: with a
positive rate around 3%, ROC-AUC flatters everything, and the operational
question is "of the merchants I flag, how many are real", which is precision.

**Timing** - error on ``tau``, scored only over merchants that were genuinely
constrained *and* whose constraint time was recorded. Imputing a time for nodes
the model missed would fold detection failure into the timing number, and recall
already measures that. Alongside the error, a concordance index: of all pairs of
victims, how often does the model put the earlier one earlier? That is the part
of timing an operator actually uses, and it survives a systematic offset that
would wreck the MAE.

**Calibration** - from :mod:`lce.learning.calibration`. A model can rank
perfectly and still be useless to act on.

Pooling
-------
Metrics are computed on the pooled node-level rows across every example in a
split, with the ``in_universe`` mask applied. A macro average over examples is
also reported: pooling lets a single large or unusually severe scenario dominate,
and the macro number says whether the result is broad or carried by one draw.

Uncertainty
-----------
Downstream positives are rare - well under one percent of the scored universe -
so a bare PR-AUC on a held-out block is not a measurement anyone should rank
models by. Every headline figure therefore carries a **clustered bootstrap**
interval: scenarios are resampled with replacement, not nodes. Nodes inside one
scenario share a network, a shock and a baseline run, so resampling them
independently would treat one cascade as dozens of observations and report an
interval several times too narrow.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lce.evaluation.metrics import average_precision, roc_auc
from lce.learning.baselines import ExampleForecast
from lce.learning.calibration import Calibrator, assess_calibration
from lce.learning.dataset import ContagionExample
from lce.learning.problem import DEFAULT_TASK, PredictionTask
from lce.logging import get_logger

logger = get_logger(__name__)

_EPS = 1e-12


def pooled(
    examples: Sequence[ContagionExample],
    forecasts: Sequence[ExampleForecast],
    *,
    horizon: float | None = None,
    calibrator: Calibrator | None = None,
    downstream_only: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """``(scores, labels)`` pooled over a split, restricted to the scored universe.

    ``horizon`` slices both sides consistently: the prediction becomes
    ``F_i(t)`` and the label becomes "constrained within ``t``". Slicing only one
    of them is the classic way to accidentally report a model as early or late.
    """
    scores: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    for example, forecast in zip(examples, forecasts, strict=True):
        mask = example.downstream_mask() if downstream_only else example.universe_mask()
        raw = forecast.score if horizon is None else forecast.probability_by(horizon)
        if calibrator is not None:
            raw = calibrator.transform(raw)
        scores.append(np.asarray(raw)[mask])
        labels.append(
            (example.y if horizon is None else example.labels_at(horizon))[mask]
        )
    if not scores:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(scores), np.concatenate(labels)


def bootstrap_pr_auc(
    examples: Sequence[ContagionExample],
    forecasts: Sequence[ExampleForecast],
    *,
    downstream_only: bool = False,
    calibrator: Calibrator | None = None,
    n_resamples: int = 1000,
    seed: int = 20250101,
    alpha: float = 0.05,
) -> dict[str, float | int] | None:
    """Percentile bootstrap interval for PR-AUC, resampling whole scenarios.

    The cluster is the scenario. One shock on one network produces a few hundred
    correlated node-level rows, so an i.i.d. bootstrap over rows would count a
    single cascade many times over and hand back an interval that looks far more
    precise than the evidence supports. Resampling scenarios keeps the unit of
    independence honest.

    Returns ``None`` when no resample contains a positive, which is the correct
    answer for a block too thin to bound rather than a number to quote.
    """
    if not examples:
        return None
    rng = np.random.default_rng(seed)
    n = len(examples)
    values: list[float] = []
    for _ in range(n_resamples):
        draw = rng.integers(0, n, size=n)
        scores, labels = pooled(
            [examples[i] for i in draw],
            [forecasts[i] for i in draw],
            calibrator=calibrator,
            downstream_only=downstream_only,
        )
        value = average_precision(scores, labels)
        if value is not None:
            values.append(value)
    if not values:
        return None
    array = np.array(values)
    return {
        "lo": float(np.quantile(array, alpha / 2.0)),
        "hi": float(np.quantile(array, 1.0 - alpha / 2.0)),
        "median": float(np.median(array)),
        "n_resamples": int(array.size),
    }


def precision_at_k(scores: np.ndarray, labels: np.ndarray, k: int) -> float | None:
    """Share of the top ``k`` ranked merchants that were genuinely affected."""
    if scores.size == 0 or k <= 0:
        return None
    k = min(k, scores.size)
    top = np.argsort(-scores, kind="stable")[:k]
    return float(labels[top].mean())


def concordance_index(
    predicted: np.ndarray, actual: np.ndarray
) -> tuple[float | None, int]:
    """Fraction of comparable victim pairs the model orders correctly.

    Ties in the prediction score half, which is the standard convention and stops
    a model that outputs one constant time from scoring 1.0 by accident.
    """
    n = predicted.size
    if n < 2:
        return None, 0
    concordant = 0.0
    comparable = 0
    for i in range(n):
        for j in range(i + 1, n):
            if actual[i] == actual[j]:
                continue
            comparable += 1
            earlier_actual = i if actual[i] < actual[j] else j
            later_actual = j if earlier_actual == i else i
            if predicted[earlier_actual] < predicted[later_actual]:
                concordant += 1.0
            elif predicted[earlier_actual] == predicted[later_actual]:
                concordant += 0.5
    if comparable == 0:
        return None, 0
    return concordant / comparable, comparable


def best_f1_threshold(scores: np.ndarray, labels: np.ndarray) -> tuple[float, float]:
    """Threshold maximising F1 on the given split, and the F1 it achieves.

    Chosen on validation and then applied unchanged to test. A threshold tuned on
    test is not a threshold, it is a second fit.
    """
    if scores.size == 0 or labels.sum() == 0:
        return 0.5, 0.0
    order = np.argsort(-scores, kind="stable")
    sorted_labels = labels[order]
    tp = np.cumsum(sorted_labels)
    predicted = np.arange(1, scores.size + 1)
    total_positive = float(labels.sum())
    precision = tp / predicted
    recall = tp / total_positive
    f1 = np.where(precision + recall > 0, 2 * precision * recall / (precision + recall + _EPS), 0.0)
    best = int(np.argmax(f1))
    return float(scores[order][best]), float(f1[best])


@dataclass(slots=True)
class TimingScore:
    """Error on the predicted time-to-constraint, over recorded victims only."""

    n_compared: int = 0
    mae_hours: float | None = None
    rmse_hours: float | None = None
    median_abs_error_hours: float | None = None
    bias_hours: float | None = None
    within_6h: float | None = None
    within_24h: float | None = None
    concordance: float | None = None
    n_comparable_pairs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_compared": self.n_compared,
            "mae_hours": self.mae_hours,
            "rmse_hours": self.rmse_hours,
            "median_abs_error_hours": self.median_abs_error_hours,
            "bias_hours": self.bias_hours,
            "within_6h": self.within_6h,
            "within_24h": self.within_24h,
            "concordance": self.concordance,
            "n_comparable_pairs": self.n_comparable_pairs,
        }


def score_timing(
    examples: Sequence[ContagionExample], forecasts: Sequence[ExampleForecast]
) -> TimingScore:
    """Timing error pooled across a split.

    Note the scope: merchants with ``y = 1``, ``timing_observed = 1`` and inside
    the universe. Nodes the model never flagged are still included - the question
    is how well it times the events that happened, not how well it times the ones
    it happened to notice.
    """
    predicted: list[np.ndarray] = []
    actual: list[np.ndarray] = []
    for example, forecast in zip(examples, forecasts, strict=True):
        mask = (example.y > 0) & (example.timing_observed > 0) & example.universe_mask()
        if not mask.any():
            continue
        predicted.append(forecast.expected_tau()[mask])
        actual.append(example.tau[mask])
    if not predicted:
        return TimingScore()

    pred = np.concatenate(predicted)
    real = np.concatenate(actual)
    error = pred - real
    absolute = np.abs(error)
    concordance, comparable = concordance_index(pred, real)
    return TimingScore(
        n_compared=int(pred.size),
        mae_hours=float(absolute.mean()),
        rmse_hours=float(np.sqrt(np.mean(error**2))),
        median_abs_error_hours=float(np.median(absolute)),
        bias_hours=float(error.mean()),
        within_6h=float(np.mean(absolute <= 6.0)),
        within_24h=float(np.mean(absolute <= 24.0)),
        concordance=concordance,
        n_comparable_pairs=comparable,
    )


@dataclass(slots=True)
class ScoreCard:
    """One model, one split, every number."""

    model: str
    split: str
    n_examples: int
    n_nodes: int
    n_positive: int
    positive_rate: float

    pr_auc: float | None = None
    pr_auc_ci: dict[str, float | int] | None = None
    roc_auc: float | None = None
    pr_auc_macro: float | None = None
    precision_at_r: float | None = None
    precision_at_10: float | None = None
    threshold: float = 0.5
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None

    downstream: dict[str, Any] = field(default_factory=dict)
    timing: TimingScore = field(default_factory=TimingScore)
    calibration: dict[str, Any] = field(default_factory=dict)
    by_horizon: dict[str, dict[str, float | None]] = field(default_factory=dict)
    by_family: dict[str, dict[str, float | None]] = field(default_factory=dict)
    calibrator: dict[str, Any] = field(default_factory=dict)

    def headline(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "split": self.split,
            "n_positive": self.n_positive,
            "pr_auc": self.pr_auc,
            "pr_auc_ci": self.pr_auc_ci,
            "roc_auc": self.roc_auc,
            "precision_at_r": self.precision_at_r,
            "downstream_pr_auc": self.downstream.get("pr_auc"),
            "downstream_pr_auc_ci": self.downstream.get("pr_auc_ci"),
            "f1": self.f1,
            "brier": self.calibration.get("brier"),
            "ece": self.calibration.get("ece"),
            "timing_mae_hours": self.timing.mae_hours,
            "concordance": self.timing.concordance,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "split": self.split,
            "n_examples": self.n_examples,
            "n_nodes": self.n_nodes,
            "n_positive": self.n_positive,
            "positive_rate": self.positive_rate,
            "pr_auc": self.pr_auc,
            "pr_auc_ci": self.pr_auc_ci,
            "roc_auc": self.roc_auc,
            "pr_auc_macro": self.pr_auc_macro,
            "precision_at_r": self.precision_at_r,
            "precision_at_10": self.precision_at_10,
            "threshold": self.threshold,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "downstream": self.downstream,
            "timing": self.timing.to_dict(),
            "calibration": self.calibration,
            "calibrator": self.calibrator,
            "by_horizon": self.by_horizon,
            "by_family": self.by_family,
        }


def evaluate_forecasts(
    examples: Sequence[ContagionExample],
    forecasts: Sequence[ExampleForecast],
    *,
    model: str,
    split: str,
    task: PredictionTask = DEFAULT_TASK,
    calibrator: Calibrator | None = None,
    threshold: float = 0.5,
    n_bootstrap: int = 1000,
    bootstrap_seed: int = 20250101,
) -> ScoreCard:
    """Score one model's forecasts on one split.

    ``n_bootstrap = 0`` skips the interval, which the ablation sweep uses: it
    compares deltas between models on the *same* resampling-free block, and
    bootstrapping every variant would triple its runtime for a number the delta
    already conveys.
    """
    scores, labels = pooled(examples, forecasts, calibrator=calibrator)
    n_positive = int(labels.sum())

    card = ScoreCard(
        model=model,
        split=split,
        n_examples=len(examples),
        n_nodes=int(labels.size),
        n_positive=n_positive,
        positive_rate=float(labels.mean()) if labels.size else 0.0,
        threshold=threshold,
    )
    if labels.size == 0:
        return card

    card.pr_auc = average_precision(scores, labels)
    card.roc_auc = roc_auc(scores, labels)
    if n_bootstrap:
        card.pr_auc_ci = bootstrap_pr_auc(
            examples,
            forecasts,
            calibrator=calibrator,
            n_resamples=n_bootstrap,
            seed=bootstrap_seed,
        )
    card.precision_at_r = precision_at_k(scores, labels, n_positive)
    card.precision_at_10 = precision_at_k(scores, labels, 10)

    predicted = scores >= threshold
    true_positive = float(np.sum(predicted & (labels > 0)))
    card.precision = float(true_positive / max(predicted.sum(), 1))
    card.recall = float(true_positive / max(n_positive, 1))
    card.f1 = (
        2 * card.precision * card.recall / (card.precision + card.recall)
        if (card.precision + card.recall) > 0
        else 0.0
    )

    # Macro: one PR-AUC per example, averaged. Guards against a single severe
    # scenario carrying the pooled number.
    per_example = []
    for example, forecast in zip(examples, forecasts, strict=True):
        one_score, one_label = pooled([example], [forecast], calibrator=calibrator)
        value = average_precision(one_score, one_label)
        if value is not None:
            per_example.append(value)
    card.pr_auc_macro = float(np.mean(per_example)) if per_example else None

    # The headline number for contagion: origins excluded. A directly-shocked
    # merchant is both trivially identifiable and usually a positive, so leaving
    # it in lets a model score well by echoing its own input back.
    down_scores, down_labels = pooled(
        examples, forecasts, calibrator=calibrator, downstream_only=True
    )
    card.downstream = {
        "pr_auc": average_precision(down_scores, down_labels),
        "roc_auc": roc_auc(down_scores, down_labels),
        "n_nodes": float(down_labels.size),
        "n_positive": float(down_labels.sum()),
        "positive_rate": float(down_labels.mean()) if down_labels.size else None,
        "precision_at_r": precision_at_k(down_scores, down_labels, int(down_labels.sum())),
        "pr_auc_ci": (
            bootstrap_pr_auc(
                examples,
                forecasts,
                downstream_only=True,
                calibrator=calibrator,
                n_resamples=n_bootstrap,
                seed=bootstrap_seed,
            )
            if n_bootstrap
            else None
        ),
    }

    card.timing = score_timing(examples, forecasts)
    card.calibration = assess_calibration(scores, labels).to_dict()
    if calibrator is not None:
        card.calibrator = calibrator.to_dict()

    remaining = min(e.remaining_hours for e in examples)
    for t in task.grid_for(remaining):
        slice_scores, slice_labels = pooled(
            examples, forecasts, horizon=t, calibrator=calibrator
        )
        card.by_horizon[f"{t:.0f}h"] = {
            "pr_auc": average_precision(slice_scores, slice_labels),
            "roc_auc": roc_auc(slice_scores, slice_labels),
            "n_positive": float(slice_labels.sum()),
        }

    families = sorted({e.family for e in examples})
    for family in families:
        pairs = [
            (e, f) for e, f in zip(examples, forecasts, strict=True) if e.family == family
        ]
        family_scores, family_labels = pooled(
            [e for e, _ in pairs], [f for _, f in pairs], calibrator=calibrator
        )
        card.by_family[family] = {
            "pr_auc": average_precision(family_scores, family_labels),
            "roc_auc": roc_auc(family_scores, family_labels),
            "n_positive": float(family_labels.sum()),
            "n_examples": float(len(pairs)),
        }

    logger.info("phase3_scored", **card.headline())
    return card


@dataclass(slots=True)
class LearningReport:
    """Every model on every split, plus the split audit that licenses the numbers."""

    cards: list[ScoreCard] = field(default_factory=list)
    split_audit: dict[str, Any] = field(default_factory=dict)
    corpus: dict[str, Any] = field(default_factory=dict)
    dependency: dict[str, Any] = field(default_factory=dict)
    ablations: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    def for_split(self, split: str) -> list[ScoreCard]:
        return [c for c in self.cards if c.split == split]

    def leaderboard(
        self, split: str = "test", *, key: str = "pr_auc"
    ) -> list[dict[str, Any]]:
        """Headlines for one split, ranked by ``key`` descending.

        ``downstream_pr_auc`` is usually the one to rank on: the pooled figure is
        dominated by the directly-shocked merchants, who are both trivially
        identifiable and usually victims, so it rewards echoing the input back.
        """
        rows = [c.headline() for c in self.for_split(split)]
        return sorted(rows, key=lambda r: (-(r.get(key) or 0.0), r["model"]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus": self.corpus,
            "split_audit": self.split_audit,
            "cards": [c.to_dict() for c in self.cards],
            "leaderboard": self.leaderboard(),
            "dependency": self.dependency,
            "ablations": self.ablations,
            "elapsed_ms": self.elapsed_ms,
        }
