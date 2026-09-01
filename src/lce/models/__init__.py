"""Learning layer: dependency inference, propagation prediction, artifacts.

Three predictors implement the same contract - ``predict(graph, shock) ->
ModelPrediction`` - so the evaluation harness can score them interchangeably:

``LinearThresholdPropagator``  analytic, magnitude-carrying, no training
``HawkesCascadePredictor``     analytic, probability-only, no training
``TemporalGNNPredictor``       learned from simulated cascades (needs the `ml` extra)
"""

from __future__ import annotations

from lce.models.dependency import (
    DependencyLearner,
    DependencyLearnerConfig,
    compare_to_ground_truth,
    learn_dependencies,
)
from lce.models.features import (
    classify_recurrence,
    compute_edge_features,
    estimate_reliability,
    fit_lag_distribution,
    regularity_score,
)
from lce.models.hawkes import MarkedHawkesFit, fit_marked_hawkes
from lce.models.propagation import (
    HawkesCascadePredictor,
    LinearThresholdPropagator,
    PropagationConfig,
)
from lce.models.registry import ModelManifest, ModelRegistry

__all__ = [
    "DependencyLearner",
    "DependencyLearnerConfig",
    "HawkesCascadePredictor",
    "LinearThresholdPropagator",
    "MarkedHawkesFit",
    "ModelManifest",
    "ModelRegistry",
    "PropagationConfig",
    "classify_recurrence",
    "compare_to_ground_truth",
    "compute_edge_features",
    "estimate_reliability",
    "fit_lag_distribution",
    "fit_marked_hawkes",
    "learn_dependencies",
    "regularity_score",
]


def __getattr__(name: str) -> object:
    """Expose the torch-dependent predictor lazily.

    Importing ``lce.models`` must not require the ``ml`` extra, so the GNN is
    resolved only when actually referenced.
    """
    if name in {"TemporalGNNPredictor", "TGNNConfig", "TrainingSample", "make_sample"}:
        from lce.models import tgnn

        return getattr(tgnn, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
