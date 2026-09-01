"""Phase-3 learning layer: predicting contagion from observables alone.

The contract this package enforces is a barrier. A model receives a
:class:`~lce.learning.dataset.ContagionExample`, whose features are a function of
the observable filtration at the prediction origin and nothing else; the latent
parameters that generated the network, the shock-perturbed obligation book and
every event at or after the origin stay on the other side, in
:class:`~lce.learning.dataset.HiddenTruth`, and are read only to score.

Layered in the order they were built:

``problem``      what is observed, what is forbidden, and the audits that check it
``features``     leak-free node, interval and pair feature tables
``dataset``      scenarios plus Phase-2 ground truth, turned into examples
``splits``       origin-time train/validation/test partitioning with verification
``baselines``    prevalence, cash-cover, shock-distance, discrete-time hazard
``pointprocess`` marked-Hawkes structure estimation plus mechanistic propagation
``graphmodel``   the Phase-1 GATv2 trunk, driven by the leak-free tables
``calibration``  isotonic and Platt recalibration, fitted on validation only
``evaluation``   discrimination, timing and calibration, reported together
``ablations``    which part of the system is doing the work
``experiment``   the protocol, start to finish, in the order it must run
"""

from __future__ import annotations

from lce.learning.baselines import (
    CLASSICAL_MODELS,
    CashCoverBaseline,
    ContagionModel,
    DiscreteTimeHazard,
    ExampleForecast,
    PrevalenceBaseline,
    ShockDistanceBaseline,
)
from lce.learning.calibration import (
    CalibrationReport,
    IsotonicCalibrator,
    PlattCalibrator,
    assess_calibration,
    select_calibrator,
)
from lce.learning.dataset import (
    ContagionExample,
    ExampleCorpus,
    HiddenTruth,
    build_corpus,
    build_dataset_examples,
    load_corpus,
    save_corpus,
)
from lce.learning.evaluation import (
    LearningReport,
    ScoreCard,
    best_f1_threshold,
    bootstrap_pr_auc,
    evaluate_forecasts,
    pooled,
)
from lce.learning.experiment import (
    Phase3Config,
    build_models,
    quick_config,
    run_phase3,
    score_on_split,
)
from lce.learning.features import (
    NODE_FEATURE_NAMES,
    ObservedStats,
    build_node_features,
    build_pair_features,
    feature_summary,
    network_free_mask,
)
from lce.learning.pointprocess import (
    HawkesContagionModel,
    HawkesDependencyEstimator,
    SupervisedDependencyRegressor,
    evaluate_dependency_recovery,
)
from lce.learning.problem import (
    DEFAULT_OBSERVATION,
    DEFAULT_TASK,
    LATENT_PROFILE_FIELDS,
    OBSERVABLE_PROFILE_FIELDS,
    LeakageAudit,
    ObservationSpec,
    ObservedWindow,
    PredictionTask,
    audit_leakage,
    audit_window,
    build_observed_window,
)
from lce.learning.splits import (
    SplitName,
    SplitSpec,
    TemporalSplit,
    assert_split_clean,
    make_temporal_split,
    verify_split,
)

__all__ = [
    "CLASSICAL_MODELS",
    "DEFAULT_OBSERVATION",
    "DEFAULT_TASK",
    "LATENT_PROFILE_FIELDS",
    "NODE_FEATURE_NAMES",
    "OBSERVABLE_PROFILE_FIELDS",
    "CalibrationReport",
    "CashCoverBaseline",
    "ContagionExample",
    "ContagionModel",
    "DiscreteTimeHazard",
    "ExampleCorpus",
    "ExampleForecast",
    "HawkesContagionModel",
    "HawkesDependencyEstimator",
    "HiddenTruth",
    "IsotonicCalibrator",
    "LeakageAudit",
    "LearningReport",
    "ObservationSpec",
    "ObservedStats",
    "ObservedWindow",
    "Phase3Config",
    "PlattCalibrator",
    "PredictionTask",
    "PrevalenceBaseline",
    "ScoreCard",
    "ShockDistanceBaseline",
    "SplitName",
    "SplitSpec",
    "SupervisedDependencyRegressor",
    "TemporalSplit",
    "assert_split_clean",
    "assess_calibration",
    "audit_leakage",
    "audit_window",
    "best_f1_threshold",
    "bootstrap_pr_auc",
    "build_corpus",
    "build_dataset_examples",
    "build_models",
    "build_node_features",
    "build_observed_window",
    "build_pair_features",
    "evaluate_dependency_recovery",
    "evaluate_forecasts",
    "feature_summary",
    "load_corpus",
    "make_temporal_split",
    "network_free_mask",
    "pooled",
    "quick_config",
    "run_phase3",
    "save_corpus",
    "score_on_split",
    "select_calibrator",
    "verify_split",
]


def __getattr__(name: str) -> object:
    """Expose the torch-dependent graph model lazily.

    Importing ``lce.learning`` must not require the ``ml`` extra: every classical
    and point-process model runs without it, and only the graph model needs
    torch.
    """
    if name in {"TemporalGraphModel", "GraphSampleSpec", "build_graph_sample"}:
        from lce.learning import graphmodel

        return getattr(graphmodel, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
