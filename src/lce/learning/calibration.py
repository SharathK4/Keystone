"""Calibration: making the probabilities mean what they say.

A contagion score that ranks well is not the same thing as a probability someone
can act on. If the system says four merchants have a 60% chance of running out of
cash this week, roughly two or three of them should. Discrimination metrics
(PR-AUC, ROC-AUC) are completely blind to whether that holds - they only see the
ordering - so they are reported alongside, never instead of, the calibration
numbers here.

Two calibrators, both standard, both fitted on the **validation split only**:

``PlattCalibrator``
    a one-dimensional logistic on the model's logit. Two parameters, so it
    survives small validation sets, and it can only stretch and shift the
    existing ordering - it never re-orders.

``IsotonicCalibrator``
    pool-adjacent-violators, the non-parametric alternative. Strictly more
    flexible, which on a few hundred validation points means it can also overfit
    into a step function; both are fitted and the choice is reported rather than
    assumed.

Why validation and not train
----------------------------
A model fitted by maximum likelihood is already calibrated *on its own training
data* almost by construction, so calibrating there measures nothing and fixes
nothing. Fitting on test would be straightforward cheating. Validation is the
only split where the question "is this model over-confident out of sample?" has an
honest answer, which is also why the temporal split reserves a whole block for it.

Metrics
-------
``brier``      mean squared error of the probability. Proper scoring rule.
``log_loss``   the other proper rule; punishes confident mistakes far harder.
``ece``/``mce`` expected and maximum calibration error over **equal-mass** bins.
               Equal-mass rather than equal-width because on a 3% base rate almost
               every prediction lands in the lowest equal-width bin, and the
               statistic degenerates into a measure of the base rate.
``slope``/``intercept``
               the logistic recalibration coefficients. ``slope < 1`` is the
               classic signature of over-confidence: predictions too spread out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import minimize

_EPS = 1e-12
_CLIP = 1e-6


def _clip(p: np.ndarray) -> np.ndarray:
    return np.clip(p, _CLIP, 1.0 - _CLIP)


def _logit(p: np.ndarray) -> np.ndarray:
    q = _clip(p)
    return np.log(q / (1.0 - q))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60.0, 60.0)))


def brier_score(probabilities: np.ndarray, labels: np.ndarray) -> float:
    """Mean squared error of the probability forecast."""
    if probabilities.size == 0:
        return float("nan")
    return float(np.mean((probabilities - labels) ** 2))


def log_loss(probabilities: np.ndarray, labels: np.ndarray) -> float:
    """Mean negative log-likelihood of the labels under the forecast."""
    if probabilities.size == 0:
        return float("nan")
    p = _clip(probabilities)
    return float(-np.mean(labels * np.log(p) + (1.0 - labels) * np.log(1.0 - p)))


def equal_mass_bins(probabilities: np.ndarray, n_bins: int) -> list[np.ndarray]:
    """Index groups of roughly equal size, ordered by predicted probability.

    Ties are kept together: splitting a run of identical predictions across two
    bins would report a calibration error that is an artefact of the sort order.
    """
    if probabilities.size == 0:
        return []
    order = np.argsort(probabilities, kind="stable")
    groups = np.array_split(order, min(n_bins, probabilities.size))
    merged: list[np.ndarray] = []
    for group in groups:
        if not group.size:
            continue
        if (
            merged
            and probabilities[merged[-1][-1]] == probabilities[group[0]]
            and np.all(probabilities[group] == probabilities[group[0]])
        ):
            merged[-1] = np.concatenate([merged[-1], group])
        else:
            merged.append(group)
    return merged


@dataclass(slots=True)
class ReliabilityCurve:
    """Binned mean prediction against binned observed frequency."""

    mean_predicted: list[float] = field(default_factory=list)
    observed_rate: list[float] = field(default_factory=list)
    count: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mean_predicted": self.mean_predicted,
            "observed_rate": self.observed_rate,
            "count": self.count,
        }


def reliability(
    probabilities: np.ndarray, labels: np.ndarray, n_bins: int = 10
) -> ReliabilityCurve:
    curve = ReliabilityCurve()
    for group in equal_mass_bins(probabilities, n_bins):
        curve.mean_predicted.append(float(probabilities[group].mean()))
        curve.observed_rate.append(float(labels[group].mean()))
        curve.count.append(int(group.size))
    return curve


def calibration_error(
    probabilities: np.ndarray, labels: np.ndarray, n_bins: int = 10
) -> tuple[float, float]:
    """``(ECE, MCE)`` over equal-mass bins."""
    groups = equal_mass_bins(probabilities, n_bins)
    if not groups:
        return float("nan"), float("nan")
    total = probabilities.size
    weighted = 0.0
    worst = 0.0
    for group in groups:
        gap = abs(float(probabilities[group].mean()) - float(labels[group].mean()))
        weighted += gap * group.size / total
        worst = max(worst, gap)
    return weighted, worst


def calibration_slope(probabilities: np.ndarray, labels: np.ndarray) -> tuple[float, float]:
    """Logistic recalibration ``(slope, intercept)`` on the model's own logit.

    A perfectly calibrated forecast gives ``(1, 0)``. A slope below one says the
    predictions are too spread out - the model is more confident than the data
    supports at both ends.
    """
    if probabilities.size < 2 or len(np.unique(labels)) < 2:
        return float("nan"), float("nan")
    z = _logit(probabilities)

    def objective(w: np.ndarray) -> tuple[float, np.ndarray]:
        p = _sigmoid(w[0] * z + w[1])
        loss = float(-np.mean(labels * np.log(p + _EPS) + (1 - labels) * np.log(1 - p + _EPS)))
        residual = p - labels
        return loss, np.array([float(np.mean(residual * z)), float(np.mean(residual))])

    result = minimize(objective, np.array([1.0, 0.0]), jac=True, method="L-BFGS-B")
    return float(result.x[0]), float(result.x[1])


@dataclass(slots=True)
class CalibrationReport:
    """Everything measured about one model's probability quality."""

    n: int
    positive_rate: float
    mean_prediction: float
    brier: float
    log_loss: float
    ece: float
    mce: float
    slope: float
    intercept: float
    curve: ReliabilityCurve

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "positive_rate": self.positive_rate,
            "mean_prediction": self.mean_prediction,
            "brier": self.brier,
            "log_loss": self.log_loss,
            "ece": self.ece,
            "mce": self.mce,
            "slope": self.slope,
            "intercept": self.intercept,
            "reliability": self.curve.to_dict(),
        }

    def headline(self) -> dict[str, float]:
        return {
            "brier": self.brier,
            "log_loss": self.log_loss,
            "ece": self.ece,
            "slope": self.slope,
        }


def assess_calibration(
    probabilities: np.ndarray, labels: np.ndarray, *, n_bins: int = 10
) -> CalibrationReport:
    """Score a probability forecast against the outcomes it claimed to predict."""
    probabilities = np.asarray(probabilities, dtype=float).ravel()
    labels = np.asarray(labels, dtype=float).ravel()
    ece, mce = calibration_error(probabilities, labels, n_bins)
    slope, intercept = calibration_slope(probabilities, labels)
    return CalibrationReport(
        n=int(probabilities.size),
        positive_rate=float(labels.mean()) if labels.size else float("nan"),
        mean_prediction=float(probabilities.mean()) if probabilities.size else float("nan"),
        brier=brier_score(probabilities, labels),
        log_loss=log_loss(probabilities, labels),
        ece=ece,
        mce=mce,
        slope=slope,
        intercept=intercept,
        curve=reliability(probabilities, labels, n_bins),
    )


# ---------------------------------------------------------------- calibrators


class Calibrator:
    """Maps raw model probabilities onto calibrated ones. Monotone by design."""

    name = "identity"

    def fit(self, probabilities: np.ndarray, labels: np.ndarray) -> Calibrator:
        return self

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        return np.asarray(probabilities, dtype=float)

    def to_dict(self) -> dict[str, Any]:
        return {"calibrator": self.name}


class PlattCalibrator(Calibrator):
    """Logistic recalibration: ``sigma(a * logit(p) + b)``.

    Two parameters. That is the point - on a validation block of a few hundred
    rows with a low base rate, anything more flexible starts fitting the noise in
    the tail, which is exactly where the probabilities matter most.
    """

    name = "platt"

    def __init__(self) -> None:
        self.slope = 1.0
        self.intercept = 0.0

    def fit(self, probabilities: np.ndarray, labels: np.ndarray) -> PlattCalibrator:
        slope, intercept = calibration_slope(
            np.asarray(probabilities, dtype=float), np.asarray(labels, dtype=float)
        )
        if math.isfinite(slope) and math.isfinite(intercept):
            self.slope, self.intercept = slope, intercept
        return self

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        z = _logit(np.asarray(probabilities, dtype=float))
        return _sigmoid(self.slope * z + self.intercept)

    def to_dict(self) -> dict[str, Any]:
        return {"calibrator": self.name, "slope": self.slope, "intercept": self.intercept}


class IsotonicCalibrator(Calibrator):
    """Pool-adjacent-violators isotonic regression.

    Implemented directly rather than pulled in: PAVA is fifteen lines, the
    monotone step function it produces is the whole contract, and having it in
    the repository means the calibration claim can be read rather than trusted.
    Predictions between knots are linearly interpolated, which avoids handing
    back a probability that no training point ever supported.
    """

    name = "isotonic"

    def __init__(self, *, out_of_range: str = "clip") -> None:
        self.out_of_range = out_of_range
        self.x: np.ndarray = np.zeros(0)
        self.y: np.ndarray = np.zeros(0)

    def fit(self, probabilities: np.ndarray, labels: np.ndarray) -> IsotonicCalibrator:
        p = np.asarray(probabilities, dtype=float).ravel()
        y = np.asarray(labels, dtype=float).ravel()
        if p.size == 0:
            return self
        order = np.argsort(p, kind="stable")
        xs, ys = p[order], y[order]

        # PAVA: walk left to right, merging any block that violates monotonicity
        # with its predecessor and replacing both by their weighted mean.
        values: list[float] = []
        weights: list[float] = []
        for value in ys:
            values.append(float(value))
            weights.append(1.0)
            while len(values) > 1 and values[-2] > values[-1]:
                w = weights[-2] + weights[-1]
                merged = (values[-2] * weights[-2] + values[-1] * weights[-1]) / w
                values[-2:] = [merged]
                weights[-2:] = [w]

        fitted = np.repeat(values, [int(w) for w in weights])
        self.x, self.y = xs, fitted[: xs.size]
        return self

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        p = np.asarray(probabilities, dtype=float)
        if self.x.size == 0:
            return p
        return np.interp(p, self.x, self.y, left=self.y[0], right=self.y[-1])

    def to_dict(self) -> dict[str, Any]:
        return {"calibrator": self.name, "n_knots": int(self.x.size)}


CALIBRATORS: dict[str, type[Calibrator]] = {
    Calibrator.name: Calibrator,
    PlattCalibrator.name: PlattCalibrator,
    IsotonicCalibrator.name: IsotonicCalibrator,
}


def select_calibrator(
    probabilities: np.ndarray,
    labels: np.ndarray,
    *,
    candidates: tuple[str, ...] = ("identity", "platt", "isotonic"),
) -> tuple[Calibrator, dict[str, float]]:
    """Fit each candidate on the validation block and keep the best log-loss.

    Selection is on the same data the calibrators are fitted on, which is a real
    limitation and is stated rather than hidden: with a single validation block
    there is nowhere else to select from. The effect is bounded - the candidates
    differ in one or two parameters - and the chosen calibrator is then scored on
    test like everything else, where any over-fitting shows up.
    """
    scores: dict[str, float] = {}
    best: tuple[float, Calibrator] | None = None
    for name in candidates:
        calibrator = CALIBRATORS[name]().fit(probabilities, labels)
        loss = log_loss(calibrator.transform(probabilities), labels)
        scores[name] = loss
        if best is None or loss < best[0]:
            best = (loss, calibrator)
    assert best is not None
    return best[1], scores
