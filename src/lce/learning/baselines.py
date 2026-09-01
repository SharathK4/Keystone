"""Classical baselines - the numbers every later model has to beat.

Four models, in increasing order of what they are allowed to know. They exist
because a contagion result is only interesting relative to what you get without
contagion modelling, and that reference has to be built properly rather than
sketched:

``PrevalenceBaseline``
    knows the base rate and the shape of the timing distribution, nothing else.
    Fixes PR-AUC at the prevalence and ROC-AUC at 0.5. Anything that fails to
    beat this is not predicting.

``CashCoverBaseline``
    the treasurer's arithmetic, and nothing else: is what I owe by time ``t``
    larger than the cash I hold plus what I am owed by then? No network, no
    propagation. This is the honest "you did not need a graph model" hypothesis,
    and it is a strong one - most first-ring victims are visible this way.

``ShockDistanceBaseline``
    the opposite ablation: pure structure, no balance sheet. How many hops from
    the shock, and how much of my inflow value traces back to it.

``DiscreteTimeHazard``
    the real classical competitor: a regularised discrete-time survival model on
    the whole leak-free feature set. Native ``F_i(t)`` *and* ``tau_hat_i`` from
    one likelihood.

Fitting criteria
----------------
The hazard model maximises the proper censored survival likelihood

.. math::

    \\ell = \\sum_i \\Big[ \\sum_{k<k_i} \\log(1-h_{ik})
            \\;+\\; \\delta_i \\log h_{ik_i} \\Big]

The two heuristics are fitted by *pooled* logistic regression on the cumulative
targets ``1{tau <= end of interval k}``. That is a fitting criterion, not a
likelihood - the repeated rows per node are correlated and it ignores that - so
they are reported as calibrated heuristics rather than as probabilistic models.
The distinction is why the calibration layer exists.

No class reweighting
--------------------
Positives are a few percent of the universe, and the usual reflex is to weight
them up. Deliberately not done here: reweighting shifts every predicted
probability away from the empirical rate, and this system's output is supposed to
be a probability someone can act on. Ranking metrics are unaffected by the
choice, and :mod:`lce.learning.calibration` handles the rest.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import minimize

from lce.domain.enums import PredictorKind
from lce.domain.prediction import ModelPrediction, NodeExposure
from lce.errors import ModelError
from lce.learning.dataset import ContagionExample
from lce.learning.features import INTERVAL_FEATURE_INDEX, NODE_FEATURE_INDEX
from lce.learning.problem import DEFAULT_TASK, PredictionTask
from lce.logging import get_logger
from lce.seeds import config_hash

logger = get_logger(__name__)

_EPS = 1e-9


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -60.0, 60.0)))


# ------------------------------------------------------------------- forecasts


@dataclass(slots=True)
class ExampleForecast:
    """One model's answer for one example: a CDF per merchant over the window.

    Everything the evaluation needs derives from ``cdf``: the exposure score is
    its last column, the timing estimate is the mean of the implied conditional
    density, and any intermediate horizon is read off by interpolation. Keeping a
    single object rather than separate score and time arrays is what stops the
    two drifting apart - a model cannot claim a node is safe and also predict
    when it fails.
    """

    scenario_id: str
    merchant_ids: list[str]
    interval_edges: np.ndarray
    cdf: np.ndarray
    origin_t: float = 0.0
    hazard: np.ndarray | None = None
    model_name: str = ""

    def __post_init__(self) -> None:
        # Monotonicity is a property of a CDF, not a hope. Heuristic scores can
        # dip when a large receivable lands mid-window; enforcing the running
        # maximum keeps "probability of failing by t" from ever decreasing in t.
        self.cdf = np.clip(np.maximum.accumulate(self.cdf, axis=1), 0.0, 1.0)

    @property
    def score(self) -> np.ndarray:
        """``F_i(T - t_0)``: probability of being constrained inside the window."""
        return self.cdf[:, -1]

    def probability_by(self, t: float) -> np.ndarray:
        """``F_i(t)`` for an arbitrary horizon, linear between interval ends."""
        ends = self.interval_edges[1:]
        knots = np.concatenate(([0.0], ends))
        values = np.concatenate((np.zeros((self.cdf.shape[0], 1)), self.cdf), axis=1)
        return np.array(
            [np.interp(t, knots, values[i]) for i in range(values.shape[0])]
        )

    def expected_tau(self) -> np.ndarray:
        """``E[tau | constrained]`` in hours from the origin.

        Conditional on the event, deliberately. An unconditional expectation
        would be dragged toward the horizon by every node the model thinks is
        safe, and would then be compared against ground-truth times that only
        exist for nodes that were actually hit.
        """
        ends = self.interval_edges[1:]
        starts = self.interval_edges[:-1]
        mids = 0.5 * (starts + ends)
        density = np.diff(self.cdf, axis=1, prepend=0.0)
        mass = density.sum(axis=1)
        weighted = density @ mids
        return np.where(mass > _EPS, weighted / np.maximum(mass, _EPS), float(mids[-1]))

    def to_model_prediction(
        self,
        *,
        predictor: PredictorKind,
        model_version: str,
        horizon_hours: float,
        threshold: float = 0.5,
        run_id: str | None = None,
        shock_id: str | None = None,
    ) -> ModelPrediction:
        """Wrap as the Phase-1 prediction contract, so the existing harness scores it."""
        scores = self.score
        taus = self.expected_tau()
        exposures = {
            merchant_id: NodeExposure(
                merchant_id=merchant_id,
                exposure_score=float(np.clip(scores[i], 0.0, 1.0)),
                expected_hit_t=float(self.origin_t + taus[i]),
            )
            for i, merchant_id in enumerate(self.merchant_ids)
        }
        return ModelPrediction(
            run_id=run_id,
            shock_id=shock_id,
            predictor=predictor,
            model_version=model_version,
            horizon_hours=horizon_hours,
            threshold=threshold,
            exposures=exposures,
            metadata={"model": self.model_name, "scenario_id": self.scenario_id},
        )


# ------------------------------------------------------------- shared plumbing


def event_interval(example: ContagionExample) -> np.ndarray:
    """Index of the interval each merchant's event falls in (``K-1`` if none)."""
    edges = example.interval_edges
    k = np.searchsorted(edges, example.tau, side="right") - 1
    return np.clip(k, 0, len(edges) - 2)


def survival_masks(example: ContagionExample) -> tuple[np.ndarray, np.ndarray]:
    """``(at_risk, event)`` masks of shape ``(n, K)`` for the survival likelihood.

    A node is at risk in every interval up to and including the one its event
    lands in; a censored node is at risk throughout. Exactly one entry of
    ``event`` is set per positive node.
    """
    n_intervals = len(example.interval_edges) - 1
    k = event_interval(example)
    grid = np.arange(n_intervals)[None, :]
    positive = example.y[:, None] > 0
    at_risk = np.where(positive, grid <= k[:, None], True)
    event = positive & (grid == k[:, None])
    return at_risk.astype(np.float64), event.astype(np.float64)


def cumulative_targets(example: ContagionExample) -> np.ndarray:
    """``1{tau <= end of interval k}`` - the target the heuristics are fitted on."""
    ends = example.interval_edges[1:][None, :]
    return ((example.y[:, None] > 0) & (example.tau[:, None] <= ends + _EPS)).astype(
        np.float64
    )


def _stack(
    examples: Sequence[ContagionExample], design: Any
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Stack per-example ``(n, K, P)`` designs and masks into one long block."""
    x = np.concatenate([design(e) for e in examples], axis=0)
    at_risk = np.concatenate([survival_masks(e)[0] for e in examples], axis=0)
    event = np.concatenate([survival_masks(e)[1] for e in examples], axis=0)
    return x, at_risk, event


@dataclass(slots=True)
class _Standardiser:
    """Column standardisation fitted on the training block only."""

    mean: np.ndarray = field(default_factory=lambda: np.zeros(0))
    scale: np.ndarray = field(default_factory=lambda: np.ones(0))

    def fit(self, x: np.ndarray) -> _Standardiser:
        flat = x.reshape(-1, x.shape[-1])
        self.mean = flat.mean(axis=0)
        scale = flat.std(axis=0)
        # A column that is constant on the training block carries no information;
        # scaling it by its (zero) spread would turn rounding noise into signal.
        self.scale = np.where(scale > 1e-8, scale, 1.0)
        return self

    def apply(self, x: np.ndarray) -> np.ndarray:
        return (x - self.mean) / self.scale


def _fit_logistic(
    x: np.ndarray,
    at_risk: np.ndarray,
    event: np.ndarray,
    *,
    l2: float,
    survival: bool,
    max_iter: int = 500,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Fit weights by L-BFGS on either the survival or the pooled criterion.

    ``survival=True`` uses the censored discrete-time likelihood, where a node
    contributes only up to the interval its event lands in. ``survival=False``
    pools every ``(node, interval)`` row against the cumulative target, which is
    what the two heuristics use.

    The intercept is the last column and is left unpenalised: shrinking it toward
    zero would pull every predicted probability toward one half, which on a 3%
    base rate is a large and entirely artificial bias.
    """
    n_features = x.shape[-1]
    flat_x = x.reshape(-1, n_features)
    weight = at_risk.reshape(-1) if survival else np.ones(flat_x.shape[0])
    target = event.reshape(-1)
    total = max(weight.sum(), 1.0)

    penalty = np.full(n_features, l2)
    penalty[-1] = 0.0

    def objective(w: np.ndarray) -> tuple[float, np.ndarray]:
        z = flat_x @ w
        p = _sigmoid(z)
        loss = -np.sum(
            weight * (target * np.log(p + _EPS) + (1.0 - target) * np.log(1.0 - p + _EPS))
        )
        grad = flat_x.T @ (weight * (p - target))
        loss = loss / total + 0.5 * float(np.sum(penalty * w * w))
        grad = grad / total + penalty * w
        return float(loss), grad

    start = np.zeros(n_features)
    # Warm-start the intercept at the empirical log-odds so the optimiser does
    # not spend its budget travelling from p = 0.5 down to a 3% base rate.
    rate = float(np.sum(weight * target) / total)
    start[-1] = math.log(max(rate, 1e-4) / max(1.0 - rate, 1e-4))

    result = minimize(objective, start, jac=True, method="L-BFGS-B", options={"maxiter": max_iter})
    return result.x, {
        "converged": bool(result.success),
        "iterations": int(result.nit),
        "final_loss": float(result.fun),
        "message": str(result.message),
    }


# ---------------------------------------------------------------- base contract


class ContagionModel:
    """Common contract: fit on a list of examples, forecast one example.

    Deliberately not the Phase-1 ``predict(graph, shock)`` signature. A Phase-3
    model consumes an :class:`~lce.learning.dataset.ContagionExample`, which is
    the observable side of the barrier and nothing else;
    :meth:`ExampleForecast.to_model_prediction` converts the answer back into the
    Phase-1 contract so the existing evaluation harness still scores it.
    """

    name: str = "base"
    kind: PredictorKind = PredictorKind.LINEAR_THRESHOLD
    needs_window: bool = False

    def __init__(self, task: PredictionTask = DEFAULT_TASK) -> None:
        self.task = task
        self._fitted = False
        self.fit_report: dict[str, Any] = {}

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def model_version(self) -> str:
        return f"{self.name}-{config_hash(self.config(), length=10)}"

    def config(self) -> dict[str, Any]:
        return {"name": self.name, "task": self.task.to_dict()}

    def fit(
        self,
        train: Sequence[ContagionExample],
        validation: Sequence[ContagionExample] = (),
    ) -> dict[str, Any]:
        raise NotImplementedError

    def predict(self, example: ContagionExample) -> ExampleForecast:
        raise NotImplementedError

    def predict_all(
        self, examples: Sequence[ContagionExample]
    ) -> list[ExampleForecast]:
        return [self.predict(e) for e in examples]

    def _require_fitted(self) -> None:
        if not self._fitted:
            raise ModelError(f"{self.name} must be fitted before predicting")


# ------------------------------------------------------------------- model 0


class PrevalenceBaseline(ContagionModel):
    """Base rate plus the empirical timing shape. The floor for every metric."""

    name = "prevalence"

    def __init__(self, task: PredictionTask = DEFAULT_TASK) -> None:
        super().__init__(task)
        self.rate = 0.0
        self.interval_share: np.ndarray = np.zeros(0)

    def fit(
        self,
        train: Sequence[ContagionExample],
        validation: Sequence[ContagionExample] = (),
    ) -> dict[str, Any]:
        if not train:
            raise ModelError("cannot fit the prevalence baseline on an empty split")
        n_intervals = len(train[0].interval_edges) - 1
        counts = np.zeros(n_intervals)
        positives = 0
        universe = 0
        for example in train:
            mask = example.universe_mask()
            universe += int(mask.sum())
            positives += int(example.y[mask].sum())
            k = event_interval(example)
            for index in np.where((example.y > 0) & mask)[0]:
                counts[k[index]] += 1.0
        self.rate = positives / max(universe, 1)
        self.interval_share = (
            counts / counts.sum() if counts.sum() > 0 else np.full(n_intervals, 1.0 / n_intervals)
        )
        self._fitted = True
        self.fit_report = {
            "rate": self.rate,
            "n_positive": positives,
            "n_universe": universe,
            "interval_share": self.interval_share.tolist(),
        }
        return self.fit_report

    def predict(self, example: ContagionExample) -> ExampleForecast:
        self._require_fitted()
        cdf = np.tile(self.rate * np.cumsum(self.interval_share), (example.n_merchants, 1))
        return ExampleForecast(
            scenario_id=example.scenario_id,
            merchant_ids=example.merchant_ids,
            interval_edges=example.interval_edges,
            cdf=cdf,
            origin_t=example.origin_t,
            model_name=self.name,
        )


# ------------------------------------------------------------------- model 1


class CashCoverBaseline(ContagionModel):
    """Treasurer's arithmetic: does the book clear the buffer, by time ``t``?

    The design is three columns and an intercept - cumulative payables due,
    cumulative receivables expected, and the direct shock, all in units of the
    node's own opening buffer - plus the elapsed fraction of the window. That is
    exactly the calculation a finance team does by hand, and it is a serious
    baseline: a merchant whose committed outflows exceed its cash is in trouble
    whether or not anything upstream of it failed.

    What it structurally cannot see is *whose* receivable is about to not arrive.
    That gap is the space a contagion model has to earn its keep in.
    """

    name = "cash_cover"

    _COLUMNS = (
        "interval.cumulative_due_over_buffer",
        "interval.cumulative_receivable_over_buffer",
        "interval.elapsed_fraction",
    )

    def __init__(self, task: PredictionTask = DEFAULT_TASK, *, l2: float = 1e-3) -> None:
        super().__init__(task)
        self.l2 = l2
        self.weights: np.ndarray = np.zeros(0)
        self.standardiser = _Standardiser()

    def config(self) -> dict[str, Any]:
        return super().config() | {"l2": self.l2, "columns": list(self._COLUMNS)}

    def _design(self, example: ContagionExample) -> np.ndarray:
        cols = [INTERVAL_FEATURE_INDEX[c] for c in self._COLUMNS]
        block = example.interval_x[:, :, cols]
        shock = example.x[:, NODE_FEATURE_INDEX["shock.direct_shock_over_buffer"]]
        shock_block = np.repeat(shock[:, None, None], block.shape[1], axis=1)
        return np.concatenate([block, shock_block], axis=2)

    def fit(
        self,
        train: Sequence[ContagionExample],
        validation: Sequence[ContagionExample] = (),
    ) -> dict[str, Any]:
        if not train:
            raise ModelError("cannot fit the cash-cover baseline on an empty split")
        raw = np.concatenate([self._design(e) for e in train], axis=0)
        self.standardiser.fit(raw)
        x = _with_intercept(self.standardiser.apply(raw))
        targets = np.concatenate([cumulative_targets(e) for e in train], axis=0)
        self.weights, report = _fit_logistic(
            x, np.ones_like(targets), targets, l2=self.l2, survival=False
        )
        self._fitted = True
        self.fit_report = report | {"n_rows": int(targets.size)}
        return self.fit_report

    def predict(self, example: ContagionExample) -> ExampleForecast:
        self._require_fitted()
        x = _with_intercept(self.standardiser.apply(self._design(example)))
        return ExampleForecast(
            scenario_id=example.scenario_id,
            merchant_ids=example.merchant_ids,
            interval_edges=example.interval_edges,
            cdf=_sigmoid(x @ self.weights),
            origin_t=example.origin_t,
            model_name=self.name,
        )


# ------------------------------------------------------------------- model 2


class ShockDistanceBaseline(ContagionModel):
    """Pure structure: how far from the shock, and how exposed to it by value.

    The mirror image of :class:`CashCoverBaseline` - it knows the network and
    nothing about anyone's balance sheet. Comparing the two says whether this
    benchmark's cascades are driven by position or by capitalisation, and the
    answer is not obvious in advance.
    """

    name = "shock_distance"

    _COLUMNS = (
        "shock.hops_from_shock",
        "shock.upstream_shock_exposure",
        "shock.is_shock_origin",
        "structure.upstream_reach_2",
    )

    def __init__(self, task: PredictionTask = DEFAULT_TASK, *, l2: float = 1e-3) -> None:
        super().__init__(task)
        self.l2 = l2
        self.weights: np.ndarray = np.zeros(0)
        self.standardiser = _Standardiser()

    def config(self) -> dict[str, Any]:
        return super().config() | {"l2": self.l2, "columns": list(self._COLUMNS)}

    def _design(self, example: ContagionExample) -> np.ndarray:
        cols = [NODE_FEATURE_INDEX[c] for c in self._COLUMNS]
        n_intervals = example.interval_x.shape[1]
        static = np.repeat(example.x[:, None, cols], n_intervals, axis=1)
        elapsed = example.interval_x[
            :, :, [INTERVAL_FEATURE_INDEX["interval.elapsed_fraction"]]
        ]
        return np.concatenate([static, elapsed], axis=2)

    def fit(
        self,
        train: Sequence[ContagionExample],
        validation: Sequence[ContagionExample] = (),
    ) -> dict[str, Any]:
        if not train:
            raise ModelError("cannot fit the shock-distance baseline on an empty split")
        raw = np.concatenate([self._design(e) for e in train], axis=0)
        self.standardiser.fit(raw)
        x = _with_intercept(self.standardiser.apply(raw))
        targets = np.concatenate([cumulative_targets(e) for e in train], axis=0)
        self.weights, report = _fit_logistic(
            x, np.ones_like(targets), targets, l2=self.l2, survival=False
        )
        self._fitted = True
        self.fit_report = report | {"n_rows": int(targets.size)}
        return self.fit_report

    def predict(self, example: ContagionExample) -> ExampleForecast:
        self._require_fitted()
        x = _with_intercept(self.standardiser.apply(self._design(example)))
        return ExampleForecast(
            scenario_id=example.scenario_id,
            merchant_ids=example.merchant_ids,
            interval_edges=example.interval_edges,
            cdf=_sigmoid(x @ self.weights),
            origin_t=example.origin_t,
            model_name=self.name,
        )


# ------------------------------------------------------------------- model 3


class DiscreteTimeHazard(ContagionModel):
    """Regularised discrete-time survival on the full leak-free feature set.

    The one classical model that answers both questions from a single fit. The
    hazard is

    .. math::

        h_{ik} = \\sigma\\big( \\alpha_k + \\beta^\\top x_i + \\gamma^\\top z_{ik} \\big)

    with :math:`\\alpha_k` a free baseline per interval, :math:`x_i` the static
    node features and :math:`z_{ik}` the time-varying ones. The survival function
    is the product of ``1 - h`` and the timing estimate is the mean of the
    implied density, so ``F_i(t)`` and ``tau_hat_i`` cannot disagree with each
    other by construction.

    A free :math:`\\alpha_k` per interval matters: contagion timing is strongly
    non-uniform - the first ring fails around the first deadline after the shock -
    and forcing a constant baseline hazard would push that structure into the
    covariates, where it would be mistaken for merchant-level signal.
    """

    name = "discrete_hazard"

    def __init__(
        self,
        task: PredictionTask = DEFAULT_TASK,
        *,
        l2: float = 1e-2,
        feature_mask: np.ndarray | None = None,
    ) -> None:
        super().__init__(task)
        self.l2 = l2
        self.feature_mask = feature_mask
        self.weights: np.ndarray = np.zeros(0)
        self.standardiser = _Standardiser()

    def config(self) -> dict[str, Any]:
        return super().config() | {
            "l2": self.l2,
            "n_node_features": int(
                self.feature_mask.sum() if self.feature_mask is not None else -1
            ),
        }

    def _design(self, example: ContagionExample) -> np.ndarray:
        """``(n, K, P)``: static features, time-varying features, interval one-hot."""
        n_intervals = example.interval_x.shape[1]
        node = example.x if self.feature_mask is None else example.x[:, self.feature_mask]
        static = np.repeat(node[:, None, :], n_intervals, axis=1)
        one_hot = np.tile(np.eye(n_intervals)[None, :, :], (example.n_merchants, 1, 1))
        width = np.full(
            (example.n_merchants, n_intervals, 1),
            math.log1p(example.remaining_hours / max(n_intervals, 1)),
        )
        return np.concatenate([static, example.interval_x, one_hot, width], axis=2)

    def fit(
        self,
        train: Sequence[ContagionExample],
        validation: Sequence[ContagionExample] = (),
    ) -> dict[str, Any]:
        if not train:
            raise ModelError("cannot fit the hazard model on an empty split")
        raw, at_risk, event = _stack(train, self._design)
        self.standardiser.fit(raw)
        x = _with_intercept(self.standardiser.apply(raw))
        self.weights, report = _fit_logistic(
            x, at_risk, event, l2=self.l2, survival=True
        )
        self._fitted = True
        self.fit_report = report | {
            "n_nodes": int(raw.shape[0]),
            "n_intervals": int(raw.shape[1]),
            "n_features": int(x.shape[-1]),
            "n_events": int(event.sum()),
        }
        if validation:
            self.fit_report["validation_nll"] = self.survival_nll(validation)
        logger.info("hazard_fitted", model=self.name, **self.fit_report)
        return self.fit_report

    def hazards(self, example: ContagionExample) -> np.ndarray:
        self._require_fitted()
        x = _with_intercept(self.standardiser.apply(self._design(example)))
        return _sigmoid(x @ self.weights)

    def predict(self, example: ContagionExample) -> ExampleForecast:
        hazard = self.hazards(example)
        survival = np.cumprod(1.0 - hazard, axis=1)
        return ExampleForecast(
            scenario_id=example.scenario_id,
            merchant_ids=example.merchant_ids,
            interval_edges=example.interval_edges,
            cdf=1.0 - survival,
            origin_t=example.origin_t,
            hazard=hazard,
            model_name=self.name,
        )

    def survival_nll(self, examples: Sequence[ContagionExample]) -> float:
        """Mean censored survival negative log-likelihood, per at-risk interval."""
        total = 0.0
        weight = 0.0
        for example in examples:
            hazard = self.hazards(example)
            at_risk, event = survival_masks(example)
            total -= float(
                np.sum(
                    at_risk
                    * (
                        event * np.log(hazard + _EPS)
                        + (1.0 - event) * np.log(1.0 - hazard + _EPS)
                    )
                )
            )
            weight += float(at_risk.sum())
        return total / max(weight, 1.0)


def spread_cdf(
    example: ContagionExample,
    probability: np.ndarray,
    tau: np.ndarray,
    *,
    log_sigma: float,
) -> np.ndarray:
    """Turn ``(P(event), E[time])`` into a CDF over the example's intervals.

    Mechanistic and learned models both hand back a total probability and a
    single predicted time; the task asks for ``F_i(t)``. A log-normal spread
    around the predicted time is the natural bridge - it is the same
    multiplicative uncertainty the estimated lag laws themselves carry, one level
    up - and ``log_sigma`` is fitted once on the training split rather than
    assumed.
    """
    ends = example.interval_edges[1:][None, :]
    centre = np.maximum(tau[:, None], _EPS)
    z = (np.log(np.maximum(ends, _EPS)) - np.log(centre)) / max(log_sigma, 1e-3)
    from math import erf as _erf_scalar

    shape = 0.5 * (1.0 + np.vectorize(_erf_scalar)(z / math.sqrt(2.0)))
    return probability[:, None] * shape


def _with_intercept(x: np.ndarray) -> np.ndarray:
    """Append a constant column. Last position, matching the unpenalised slot."""
    return np.concatenate([x, np.ones((*x.shape[:-1], 1))], axis=-1)


CLASSICAL_MODELS: dict[str, type[ContagionModel]] = {
    PrevalenceBaseline.name: PrevalenceBaseline,
    CashCoverBaseline.name: CashCoverBaseline,
    ShockDistanceBaseline.name: ShockDistanceBaseline,
    DiscreteTimeHazard.name: DiscreteTimeHazard,
}
