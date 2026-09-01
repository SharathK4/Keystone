"""Temporal point-process models: learn the dependencies, then propagate.

This is the layer where the Phase-3 problem statement's third task actually
happens. Everything upstream treats the network as a fixed structure with
features attached; here the structure itself is *estimated*, from nothing but the
pre-origin payment stream, and then a mechanistic propagation rule is run over
what was estimated.

The estimator
-------------
Unchanged from Phase 1: :class:`~lce.models.dependency.DependencyLearner` fits a
**marked Hawkes process** per directed pair, treating merchant ``i``'s inflows as
the exogenous events that excite its outflows to ``j`` (see
:mod:`lce.models.hawkes` for the EM derivation). It returns three separated
quantities per link - pass-through :math:`\\theta_{ij}`, conditional trigger
probability :math:`q_{ij}`, and a log-normal lag law - and it needs no labels.

That last point is the design commitment. The main dependency experiment is
**unsupervised**: the estimator never sees a true edge, and is scored against the
generator's parameters only after the fact.
:class:`SupervisedDependencyRegressor` exists in this module as the declared
upper bound - a ridge regression *onto* the true pass-through - and is
deliberately kept out of the contagion pipeline so the two can never be confused.

From structure to a forecast
----------------------------
The learned overlay is installed on the observable graph and handed to the
Phase-1 :class:`~lce.models.propagation.LinearThresholdPropagator`, which is a
mechanistic rule with no free parameters: a node absorbs incoming shortfall up to
its buffer and passes on the excess, capped by what is actually owed on the link.

Two scalars are then fitted on the training split, and only two:

* a Platt map from the propagator's exposure score to a probability - the
  propagator's logistic is centred on "shortfall equals buffer", which is the
  right *shape* but not a calibrated rate;
* a log-normal spread around the propagator's predicted hit time, which turns a
  point estimate into the ``F_i(t)`` the task asks for.

Keeping the parameter count at two is what makes this a *point-process baseline*
rather than a second learned model wearing a mechanistic coat.

Cost
----
The EM runs per directed pair, so it is by far the most expensive model here.
Windows are content-identical whenever two scenarios on the same dataset share an
origin - which the mutation families frequently do - so fits are cached on
``(dataset, origin, observation spec)`` and reused.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import minimize

from lce.domain.edges import DependencyEdge
from lce.domain.enums import PredictorKind
from lce.errors import ModelError
from lce.graph.temporal_graph import TemporalPaymentGraph
from lce.learning.baselines import ContagionModel, ExampleForecast, spread_cdf
from lce.learning.dataset import ContagionExample, ExampleCorpus
from lce.learning.problem import DEFAULT_TASK, ObservedWindow, PredictionTask
from lce.logging import get_logger
from lce.models.dependency import (
    DependencyLearner,
    DependencyLearnerConfig,
    compare_to_ground_truth,
)
from lce.models.propagation import (
    HawkesCascadePredictor,
    LinearThresholdPropagator,
    PropagationConfig,
)

logger = get_logger(__name__)

_EPS = 1e-9


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60.0, 60.0)))


def _logit(p: np.ndarray) -> np.ndarray:
    q = np.clip(p, 1e-6, 1.0 - 1e-6)
    return np.log(q / (1.0 - q))


# ------------------------------------------------------------------ estimation


@dataclass(slots=True)
class DependencyEstimate:
    """Learned structure for one observed window."""

    edges: list[DependencyEdge] = field(default_factory=list)
    window_key: tuple[str, float] = ("", 0.0)
    n_events: int = 0

    def as_map(self) -> dict[tuple[str, str], DependencyEdge]:
        return {e.key: e for e in self.edges}


class HawkesDependencyEstimator:
    """Fits the latent dependency overlay from a window's pre-origin stream.

    A thin, cached adapter over the Phase-1 learner rather than a reimplementation:
    the estimator is the part of the system that is already validated against the
    generator, and re-deriving it here would mean two versions of the same claim.
    """

    def __init__(self, config: DependencyLearnerConfig | None = None) -> None:
        self.config = config or DependencyLearnerConfig()
        self._learner = DependencyLearner(self.config)
        self._cache: dict[tuple[str, float, str], DependencyEstimate] = {}

    def cache_key(self, window: ObservedWindow) -> tuple[str, float, str]:
        return (
            window.dataset_id,
            round(window.origin_t, 6),
            repr(sorted(window.spec.to_dict().items())),
        )

    def estimate(self, window: ObservedWindow) -> DependencyEstimate:
        """Learn the overlay, reusing a previous fit on an identical window."""
        key = self.cache_key(window)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        edges = self._learner.fit_graph(window.graph, t_end=window.origin_t)
        estimate = DependencyEstimate(
            edges=edges,
            window_key=(window.dataset_id, window.origin_t),
            n_events=window.graph.stats().n_payment_events,
        )
        self._cache[key] = estimate
        return estimate

    def install(self, window: ObservedWindow, *, grace_hours: float = 48.0) -> TemporalPaymentGraph:
        """Observable graph, rolled forward to the origin, with the learned overlay."""
        graph = window.state_graph(grace_hours=grace_hours)
        graph.clear_dependencies()
        graph.set_dependencies(self.estimate(window).edges)
        return graph

    def clear(self) -> None:
        self._cache.clear()


# ------------------------------------------------------------- contagion model


PROPAGATORS: dict[str, tuple[Any, PredictorKind]] = {
    "linear_threshold": (LinearThresholdPropagator, PredictorKind.LINEAR_THRESHOLD),
    "hawkes_cascade": (HawkesCascadePredictor, PredictorKind.HAWKES_CASCADE),
}


class HawkesContagionModel(ContagionModel):
    """Marked-Hawkes structure estimation followed by mechanistic propagation.

    ``propagator`` selects which Phase-1 rule runs on the learned overlay.
    ``linear_threshold`` carries magnitudes and absorbs them against buffers;
    ``hawkes_cascade`` is magnitude-blind and asks only whether a node is reached.
    They fail differently, which is the point of keeping both: agreement between
    them is evidence, and disagreement localises whether an error is in the
    estimated structure or in the shortfall arithmetic.
    """

    name = "hawkes_propagation"
    needs_window = True

    def __init__(
        self,
        task: PredictionTask = DEFAULT_TASK,
        *,
        propagator: str = "linear_threshold",
        estimator: HawkesDependencyEstimator | None = None,
        learner_config: DependencyLearnerConfig | None = None,
        propagation_config: PropagationConfig | None = None,
    ) -> None:
        super().__init__(task)
        if propagator not in PROPAGATORS:
            raise ModelError(f"unknown propagator {propagator!r}")
        self.propagator_name = propagator
        self.estimator = estimator or HawkesDependencyEstimator(learner_config)
        self.propagation_config = propagation_config
        self.kind = PROPAGATORS[propagator][1]
        # Two fitted scalars for the score map, one for the timing spread.
        self.score_slope = 1.0
        self.score_intercept = 0.0
        self.log_time_sigma = 0.6
        self._exposure_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    @property
    def name_with_propagator(self) -> str:
        return f"{self.name}:{self.propagator_name}"

    def config(self) -> dict[str, Any]:
        return super().config() | {
            "propagator": self.propagator_name,
            "learner": self.estimator.config.to_dict(),
        }

    # ------------------------------------------------------------- mechanics

    def _propagate(self, example: ContagionExample) -> tuple[np.ndarray, np.ndarray]:
        """``(exposure, predicted_tau)`` per merchant, in example order."""
        cached = self._exposure_cache.get(example.scenario_id)
        if cached is not None:
            return cached

        window = example.window
        if window is None:
            raise ModelError(
                f"{self.name} needs the observed window; example "
                f"{example.scenario_id!r} was loaded from disk without one"
            )
        if window.shock is None:
            raise ModelError(
                f"{self.name} needs the shock descriptor; it is disabled in this "
                "observation spec"
            )

        graph = self.estimator.install(window)
        config = self.propagation_config or PropagationConfig(
            horizon_hours=window.horizon_end
        )
        factory = PROPAGATORS[self.propagator_name][0]
        prediction = factory(config).predict(graph, window.shock)

        exposure = np.zeros(example.n_merchants)
        tau = np.full(example.n_merchants, example.remaining_hours)
        for i, merchant_id in enumerate(example.merchant_ids):
            node = prediction.exposures.get(merchant_id)
            if node is None:
                continue
            exposure[i] = node.exposure_score
            if node.expected_hit_t is not None:
                # The propagator works in absolute simulation hours; the task is
                # measured from the origin.
                tau[i] = float(
                    np.clip(
                        node.expected_hit_t - example.origin_t,
                        _EPS,
                        example.remaining_hours,
                    )
                )
        result = (exposure, tau)
        self._exposure_cache[example.scenario_id] = result
        return result

    def _cdf(
        self, example: ContagionExample, probability: np.ndarray, tau: np.ndarray
    ) -> np.ndarray:
        return spread_cdf(example, probability, tau, log_sigma=self.log_time_sigma)

    # ------------------------------------------------------------------- fit

    def fit(
        self,
        train: Sequence[ContagionExample],
        validation: Sequence[ContagionExample] = (),
    ) -> dict[str, Any]:
        """Fit the exposure-to-probability map and the timing spread.

        Three parameters over the whole training split. The structure itself is
        not fitted here - it is estimated per window, without labels - so this
        step cannot rescue a bad overlay, only re-scale a good one.
        """
        if not train:
            raise ModelError("cannot fit the point-process model on an empty split")

        exposures: list[np.ndarray] = []
        labels: list[np.ndarray] = []
        log_ratio: list[float] = []
        for example in train:
            exposure, tau = self._propagate(example)
            mask = example.universe_mask()
            exposures.append(exposure[mask])
            labels.append(example.y[mask])
            hit = (example.y > 0) & (example.timing_observed > 0) & mask
            if hit.any():
                log_ratio.extend(
                    np.log(np.maximum(example.tau[hit], _EPS))
                    - np.log(np.maximum(tau[hit], _EPS))
                )

        x = _logit(np.concatenate(exposures))
        y = np.concatenate(labels)

        def objective(w: np.ndarray) -> tuple[float, np.ndarray]:
            p = _sigmoid(w[0] * x + w[1])
            loss = float(
                -np.mean(y * np.log(p + _EPS) + (1.0 - y) * np.log(1.0 - p + _EPS))
            )
            residual = p - y
            return loss, np.array([float(np.mean(residual * x)), float(np.mean(residual))])

        result = minimize(objective, np.array([1.0, 0.0]), jac=True, method="L-BFGS-B")
        self.score_slope, self.score_intercept = float(result.x[0]), float(result.x[1])
        if log_ratio:
            self.log_time_sigma = float(np.clip(np.std(log_ratio), 0.15, 2.0))

        self._fitted = True
        self.fit_report = {
            "score_slope": self.score_slope,
            "score_intercept": self.score_intercept,
            "log_time_sigma": self.log_time_sigma,
            "n_rows": int(y.size),
            "n_positive": int(y.sum()),
            "n_timing_observations": len(log_ratio),
            "converged": bool(result.success),
            "propagator": self.propagator_name,
        }
        logger.info("hawkes_contagion_fitted", **self.fit_report)
        return self.fit_report

    def predict(self, example: ContagionExample) -> ExampleForecast:
        self._require_fitted()
        exposure, tau = self._propagate(example)
        probability = _sigmoid(self.score_slope * _logit(exposure) + self.score_intercept)
        return ExampleForecast(
            scenario_id=example.scenario_id,
            merchant_ids=example.merchant_ids,
            interval_edges=example.interval_edges,
            cdf=self._cdf(example, probability, tau),
            origin_t=example.origin_t,
            model_name=self.name_with_propagator,
        )


# ------------------------------------------------- task 3: dependency recovery


@dataclass(slots=True)
class DependencyRecoveryReport:
    """How well the latent structure was recovered, per example and pooled."""

    per_example: list[dict[str, Any]] = field(default_factory=list)
    pooled: dict[str, float] = field(default_factory=dict)
    supervised: dict[str, Any] = field(default_factory=dict)
    """The declared upper bound, or an explanation of why it could not be fitted.
    Kept in its own field so it can never be mistaken for the unsupervised result."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_examples": len(self.per_example),
            "pooled": self.pooled,
            "supervised_upper_bound": self.supervised,
            "per_example": self.per_example,
        }


def evaluate_dependency_recovery(
    corpus: ExampleCorpus,
    examples: Sequence[ContagionExample],
    *,
    estimator: HawkesDependencyEstimator | None = None,
) -> DependencyRecoveryReport:
    """Score the unsupervised estimator against the generator's true parameters.

    Nothing here feeds back into the estimator: the true edges are read out of
    :class:`~lce.learning.dataset.HiddenTruth` after the fit, purely to measure
    it. Rank correlation is the number to read - the propagation rule consumes
    the *relative* strength of links, so getting the ordering right matters more
    than getting the level right.
    """
    estimator = estimator or HawkesDependencyEstimator()
    report = DependencyRecoveryReport()

    seen: set[tuple[str, float]] = set()
    accumulator: dict[str, list[float]] = {}
    for example in examples:
        if example.window is None:
            raise ModelError(
                "dependency recovery needs observed windows; the corpus was "
                "loaded from disk without them"
            )
        key = (example.dataset_id, round(example.origin_t, 6))
        if key in seen:
            continue
        seen.add(key)

        estimate = estimator.estimate(example.window)
        truth = corpus.hidden.edges_for(example.dataset_id)
        metrics = compare_to_ground_truth(estimate.edges, truth)
        report.per_example.append(
            {
                "dataset_id": example.dataset_id,
                "origin_t": example.origin_t,
                "n_events": estimate.n_events,
                **metrics,
            }
        )
        for name, value in metrics.items():
            accumulator.setdefault(name, []).append(float(value))

    report.pooled = {
        name: float(np.mean(values)) for name, values in sorted(accumulator.items())
    }
    logger.info(
        "dependency_recovery",
        n_windows=len(report.per_example),
        pass_through_mae=report.pooled.get("pass_through_mae"),
        pass_through_spearman=report.pooled.get("pass_through_spearman"),
    )
    return report


class SupervisedDependencyRegressor:
    """The declared upper bound for Task 3: ridge regression onto the true theta.

    **This model trains on hidden labels.** It exists to answer one question -
    how much of the unsupervised estimator's error is irreducible from the
    observable pair statistics, and how much is the estimator's own - and it is
    kept out of the contagion pipeline entirely. Nothing in
    :class:`HawkesContagionModel` or the graph model may consume it.

    Fitted on the pair features of the *training* datasets against their true
    pass-through, and evaluated on the test datasets, so even the upper bound is
    an out-of-sample number rather than a fit quality.
    """

    name = "supervised_dependency"

    def __init__(self, *, l2: float = 1e-2) -> None:
        self.l2 = l2
        self.weights: np.ndarray = np.zeros(0)
        self.mean: np.ndarray = np.zeros(0)
        self.scale: np.ndarray = np.ones(0)

    def _design(
        self, corpus: ExampleCorpus, examples: Sequence[ContagionExample]
    ) -> tuple[np.ndarray, np.ndarray]:
        rows: list[np.ndarray] = []
        targets: list[float] = []
        seen: set[tuple[str, float]] = set()
        for example in examples:
            key = (example.dataset_id, round(example.origin_t, 6))
            if key in seen:
                continue
            seen.add(key)
            truth = corpus.hidden.edges_for(example.dataset_id)
            for row, pair in enumerate(example.pair_keys):
                edge = truth.get((pair[0], pair[1]))
                if edge is None:
                    continue
                rows.append(example.pair_x[row])
                targets.append(edge.pass_through)
        if not rows:
            return np.zeros((0, 1)), np.zeros(0)
        return np.vstack(rows), np.array(targets, dtype=float)

    def fit(
        self, corpus: ExampleCorpus, train: Sequence[ContagionExample]
    ) -> dict[str, Any]:
        x, y = self._design(corpus, train)
        if x.shape[0] == 0:
            raise ModelError("no matched pairs to fit the supervised upper bound on")
        self.mean = x.mean(axis=0)
        scale = x.std(axis=0)
        self.scale = np.where(scale > 1e-8, scale, 1.0)
        z = np.concatenate([(x - self.mean) / self.scale, np.ones((x.shape[0], 1))], axis=1)
        penalty = np.eye(z.shape[1]) * self.l2
        penalty[-1, -1] = 0.0
        self.weights = np.linalg.solve(z.T @ z + penalty * z.shape[0], z.T @ y)
        return {"n_pairs": int(x.shape[0]), "n_features": int(z.shape[1])}

    def score(
        self, corpus: ExampleCorpus, test: Sequence[ContagionExample]
    ) -> dict[str, float]:
        x, y = self._design(corpus, test)
        if x.shape[0] == 0:
            return {}
        z = np.concatenate([(x - self.mean) / self.scale, np.ones((x.shape[0], 1))], axis=1)
        predicted = np.clip(z @ self.weights, 0.0, 1.0)
        error = predicted - y
        return {
            "n_pairs": float(x.shape[0]),
            "pass_through_mae": float(np.mean(np.abs(error))),
            "pass_through_rmse": float(np.sqrt(np.mean(error**2))),
            "pass_through_bias": float(np.mean(error)),
            "pass_through_corr": _corr(predicted, y),
            "pass_through_spearman": _corr(_ranks(predicted), _ranks(y)),
        }


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 2 or float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _ranks(a: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(a)).astype(float)
