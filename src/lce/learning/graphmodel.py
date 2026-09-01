"""The temporal graph model, wired to the leak-free observables.

Phase 1 already contains a GATv2 contagion network
(:mod:`lce.models.tgnn`). What it does *not* contain is a way to run that
network without reading the generator's answers: its own feature builders take
merchant profiles and the true dependency overlay straight off the graph, which
is exactly what the Phase-3 barrier forbids. So this module reuses the trunk, the
training loop and the artifact format, and replaces only the inputs.

What the model sees
-------------------
``x``           the leak-free node features from :mod:`lce.learning.features`.
``edge_index``  the pairs that transacted before the origin, payer to payee -
                the contagion direction, since a starved payer is what stops a
                payee being paid.
``edge_attr``   the pair's descriptive statistics, concatenated with the four
                *estimated* dependency parameters from the marked-Hawkes fit:
                pass-through, trigger probability, mean lag and reliability.

That last part is the whole design. The graph model is not handed a structure; it
is handed the structure the point-process estimator recovered from the payment
stream, errors included. Its result is therefore a joint statement about the
estimator and the network - which is the honest thing to measure, and the reason
the ``true_structure`` ablation exists to separate the two.

Structure variants
------------------
``learned``   the estimated overlay. The real model.
``true``      the generator's overlay. **Leaky by construction** - an upper bound
              on what perfect structure recovery would buy, never a result.
``shuffled``  the estimated overlay with its targets permuted. Same degrees, same
              attribute marginals, destroyed topology: a negative control that
              catches a model scoring well off node features alone.
``none``      no edges at all, which reduces the network to an MLP on ``x``.

Labels
------
The exposure head is trained on the attributable label; the hit-time head is
trained on ``tau`` normalised by each example's own remaining horizon, and masked
to nodes whose constraint time was actually recorded. Different examples have
different remaining windows, so a shared absolute scale would teach the model that
late origins fail sooner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from lce.domain.edges import DependencyEdge
from lce.domain.enums import PredictorKind
from lce.errors import ModelError
from lce.learning.baselines import ContagionModel, ExampleForecast, spread_cdf
from lce.learning.dataset import ContagionExample
from lce.learning.features import PAIR_FEATURE_DIM
from lce.learning.pointprocess import HawkesDependencyEstimator
from lce.learning.problem import DEFAULT_TASK, PredictionTask
from lce.logging import get_logger
from lce.models.tgnn import TemporalGNNPredictor, TGNNConfig, TrainingSample

logger = get_logger(__name__)

#: Estimated dependency parameters appended to each edge's descriptive features.
DEPENDENCY_ATTR_NAMES: tuple[str, ...] = (
    "edge.pass_through",
    "edge.conditional_probability",
    "edge.log_mean_lag",
    "edge.reliability",
)
EDGE_FEATURE_DIM = PAIR_FEATURE_DIM + len(DEPENDENCY_ATTR_NAMES)

STRUCTURES: tuple[str, ...] = ("learned", "true", "shuffled", "none")


@dataclass(frozen=True, slots=True)
class GraphSampleSpec:
    """How one example is turned into a graph sample."""

    structure: str = "learned"
    seed: int = 20250101

    def __post_init__(self) -> None:
        if self.structure not in STRUCTURES:
            raise ModelError(
                f"unknown structure {self.structure!r}; expected one of {STRUCTURES}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"structure": self.structure, "seed": self.seed}


def _edge_attributes(edge: DependencyEdge | None) -> list[float]:
    if edge is None:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        float(edge.pass_through),
        float(edge.conditional_probability),
        float(np.log1p(max(edge.lag.mean_hours, 0.0)) / 10.0),
        float(edge.reliability),
    ]


def build_graph_sample(
    example: ContagionExample,
    *,
    estimator: HawkesDependencyEstimator,
    spec: GraphSampleSpec = GraphSampleSpec(),
    true_edges: dict[tuple[str, str], DependencyEdge] | None = None,
) -> TrainingSample:
    """Assemble the tensors for one example under a given structure variant."""
    if spec.structure == "true" and true_edges is None:
        raise ModelError(
            "the true-structure variant is an oracle ablation and needs the "
            "generator's edges passed in explicitly"
        )

    index = {m: i for i, m in enumerate(example.merchant_ids)}
    pair_row = {tuple(k): i for i, k in enumerate(example.pair_keys)}

    if spec.structure == "none":
        edges: dict[tuple[str, str], DependencyEdge | None] = {}
    elif spec.structure == "true":
        assert true_edges is not None
        edges = dict(true_edges)
    else:
        if example.window is None:
            raise ModelError(
                f"{spec.structure!r} structure needs the observed window; example "
                f"{example.scenario_id!r} was loaded from disk without one"
            )
        edges = {e.key: e for e in estimator.estimate(example.window).edges}

    keys = sorted(k for k in edges if k[0] in index and k[1] in index)

    targets = [k[1] for k in keys]
    if spec.structure == "shuffled" and targets:
        # Permute where the edges point while keeping who they come from. Degrees
        # and attribute marginals survive; the topology does not.
        rng = np.random.default_rng(spec.seed)
        targets = [targets[i] for i in rng.permutation(len(targets))]

    src: list[int] = []
    dst: list[int] = []
    attrs: list[list[float]] = []
    for key, target in zip(keys, targets, strict=True):
        source = key[0]
        if source == target:
            continue
        src.append(index[source])
        dst.append(index[target])
        # Attributes follow the *original* link even when the endpoint has been
        # permuted: the control is meant to keep the marginal distribution of
        # edge strengths intact and break only where they point.
        row = pair_row.get(key)
        descriptive = (
            example.pair_x[row].tolist() if row is not None else [0.0] * PAIR_FEATURE_DIM
        )
        attrs.append(descriptive + _edge_attributes(edges.get(key)))

    if src:
        edge_index = np.array([src, dst], dtype=np.int64)
        edge_attr = np.nan_to_num(
            np.array(attrs, dtype=np.float32), nan=0.0, posinf=0.0, neginf=0.0
        )
    else:
        edge_index = np.zeros((2, 0), dtype=np.int64)
        edge_attr = np.zeros((0, EDGE_FEATURE_DIM), dtype=np.float32)

    remaining = example.remaining_hours
    return TrainingSample(
        node_ids=list(example.merchant_ids),
        x=example.x.astype(np.float32),
        edge_index=edge_index,
        edge_attr=edge_attr,
        y=example.y.astype(np.float32),
        hit_time=np.clip(example.tau / remaining, 0.0, 1.0).astype(np.float32),
        hit_mask=((example.y > 0) & (example.timing_observed > 0)).astype(np.float32),
        shock_id=example.scenario_id,
    )


class TemporalGraphModel(ContagionModel):
    """GATv2 contagion network over the estimated dependency overlay."""

    name = "temporal_gnn"
    kind = PredictorKind.TEMPORAL_GNN
    needs_window = True

    def __init__(
        self,
        task: PredictionTask = DEFAULT_TASK,
        *,
        config: TGNNConfig | None = None,
        estimator: HawkesDependencyEstimator | None = None,
        sample_spec: GraphSampleSpec = GraphSampleSpec(),
        true_edges_by_dataset: dict[str, dict[tuple[str, str], DependencyEdge]] | None = None,
    ) -> None:
        super().__init__(task)
        from lce.learning.features import NODE_FEATURE_DIM

        base = config or TGNNConfig()
        self.config_obj = TGNNConfig(
            **(
                base.to_dict()
                | {
                    "node_feature_dim": NODE_FEATURE_DIM,
                    "edge_feature_dim": EDGE_FEATURE_DIM,
                    # Hit times are normalised per example, so the head's output
                    # scale is a fraction of the window, not an hour count.
                    "horizon_hours": 1.0,
                }
            )
        )
        self.estimator = estimator or HawkesDependencyEstimator()
        self.sample_spec = sample_spec
        self.true_edges_by_dataset = true_edges_by_dataset or {}
        self.predictor = TemporalGNNPredictor(self.config_obj)
        self.log_time_sigma = 0.6
        self._samples: dict[str, TrainingSample] = {}

    @property
    def name_with_structure(self) -> str:
        return f"{self.name}:{self.sample_spec.structure}"

    def config(self) -> dict[str, Any]:
        return super().config() | {
            "tgnn": self.config_obj.to_dict(),
            "sample": self.sample_spec.to_dict(),
        }

    def _sample(self, example: ContagionExample) -> TrainingSample:
        cached = self._samples.get(example.scenario_id)
        if cached is not None:
            return cached
        sample = build_graph_sample(
            example,
            estimator=self.estimator,
            spec=self.sample_spec,
            true_edges=self.true_edges_by_dataset.get(example.dataset_id),
        )
        self._samples[example.scenario_id] = sample
        return sample

    def fit(
        self,
        train: Sequence[ContagionExample],
        validation: Sequence[ContagionExample] = (),
    ) -> dict[str, Any]:
        if not train:
            raise ModelError("cannot train the temporal graph model on an empty split")
        train_samples = [self._sample(e) for e in train]
        validation_samples = [self._sample(e) for e in validation]
        report = self.predictor.fit(train_samples, validation_samples)
        self._fitted = True

        # Timing spread, fitted the same way as the point-process model: the
        # residual scatter of log(true tau / predicted tau) on the training split.
        residuals: list[float] = []
        for example in train:
            _, tau = self._raw(example)
            hit = (example.y > 0) & (example.timing_observed > 0) & example.universe_mask()
            if hit.any():
                residuals.extend(
                    np.log(np.maximum(example.tau[hit], 1e-6))
                    - np.log(np.maximum(tau[hit], 1e-6))
                )
        if residuals:
            self.log_time_sigma = float(np.clip(np.std(residuals), 0.15, 2.0))

        self.fit_report = report | {
            "structure": self.sample_spec.structure,
            "log_time_sigma": self.log_time_sigma,
            "n_edges_mean": float(
                np.mean([s.edge_index.shape[1] for s in train_samples])
            ),
        }
        logger.info("temporal_graph_fitted", **self.fit_report)
        return self.fit_report

    def _raw(self, example: ContagionExample) -> tuple[np.ndarray, np.ndarray]:
        """``(exposure, predicted tau in hours)`` straight from the two heads."""
        prediction = self.predictor.predict_from_sample(
            self._sample(example),
            shock_id=example.scenario_id,
            horizon_hours=example.remaining_hours,
        )
        exposure = np.array(
            [
                prediction.exposures[m].exposure_score
                for m in example.merchant_ids
            ]
        )
        tau = np.array(
            [
                np.clip(
                    prediction.exposures[m].expected_hit_t or example.remaining_hours,
                    1e-6,
                    example.remaining_hours,
                )
                for m in example.merchant_ids
            ]
        )
        return exposure, tau

    def predict(self, example: ContagionExample) -> ExampleForecast:
        self._require_fitted()
        exposure, tau = self._raw(example)
        return ExampleForecast(
            scenario_id=example.scenario_id,
            merchant_ids=example.merchant_ids,
            interval_edges=example.interval_edges,
            cdf=spread_cdf(example, exposure, tau, log_sigma=self.log_time_sigma),
            origin_t=example.origin_t,
            model_name=self.name_with_structure,
        )

    # ------------------------------------------------------------ persistence

    def save(self, path: Path) -> Path:
        self._require_fitted()
        return self.predictor.save(Path(path))

    def load(self, path: Path) -> TemporalGraphModel:
        self.predictor = TemporalGNNPredictor.load(Path(path))
        self.config_obj = self.predictor.config
        self._fitted = True
        return self
