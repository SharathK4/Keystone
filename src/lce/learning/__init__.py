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

Imports are lazy
----------------
Every public name resolves through :func:`__getattr__` rather than being imported
when the package loads. That is not a style choice: ``dataset`` reaches the
benchmark package and therefore the dataset *generator*, and the production
inference service must be able to build a feature table without any
data-generation or training code in the process. Eager imports here would make
that impossible from the first line, and a test asserts it stays impossible to
break.
"""

from __future__ import annotations

from typing import Any

#: Public name -> module it lives in. The single source of truth for both
#: ``__all__`` and the lazy resolver, so the two cannot drift apart.
_EXPORTS: dict[str, str] = {
    # problem
    "DEFAULT_OBSERVATION": "problem",
    "DEFAULT_TASK": "problem",
    "LATENT_PROFILE_FIELDS": "problem",
    "OBSERVABLE_PROFILE_FIELDS": "problem",
    "LeakageAudit": "problem",
    "ObservationSpec": "problem",
    "ObservedWindow": "problem",
    "PredictionTask": "problem",
    "audit_leakage": "problem",
    "audit_window": "problem",
    "build_observed_window": "problem",
    "is_observed": "problem",
    "scrub_profile": "problem",
    # features
    "NODE_FEATURE_NAMES": "features",
    "ObservedStats": "features",
    "build_node_features": "features",
    "build_interval_features": "features",
    "build_pair_features": "features",
    "feature_summary": "features",
    "network_free_mask": "features",
    # dataset
    "ContagionExample": "dataset",
    "ExampleCorpus": "dataset",
    "HiddenTruth": "dataset",
    "build_corpus": "dataset",
    "build_dataset_examples": "dataset",
    "load_corpus": "dataset",
    "save_corpus": "dataset",
    # splits
    "SplitName": "splits",
    "SplitSpec": "splits",
    "TemporalSplit": "splits",
    "assert_split_clean": "splits",
    "make_temporal_split": "splits",
    "verify_split": "splits",
    # baselines
    "CLASSICAL_MODELS": "baselines",
    "CashCoverBaseline": "baselines",
    "ContagionModel": "baselines",
    "DiscreteTimeHazard": "baselines",
    "ExampleForecast": "baselines",
    "PrevalenceBaseline": "baselines",
    "ShockDistanceBaseline": "baselines",
    # calibration
    "CalibrationReport": "calibration",
    "IsotonicCalibrator": "calibration",
    "PlattCalibrator": "calibration",
    "assess_calibration": "calibration",
    "select_calibrator": "calibration",
    # evaluation
    "LearningReport": "evaluation",
    "ScoreCard": "evaluation",
    "best_f1_threshold": "evaluation",
    "bootstrap_pr_auc": "evaluation",
    "evaluate_forecasts": "evaluation",
    "pooled": "evaluation",
    # point process
    "HawkesContagionModel": "pointprocess",
    "HawkesDependencyEstimator": "pointprocess",
    "SupervisedDependencyRegressor": "pointprocess",
    "evaluate_dependency_recovery": "pointprocess",
    # graph model (needs the ml extra)
    "GraphSampleSpec": "graphmodel",
    "TemporalGraphModel": "graphmodel",
    "build_graph_sample": "graphmodel",
    # experiment
    "Phase3Config": "experiment",
    "build_models": "experiment",
    "quick_config": "experiment",
    "run_phase3": "experiment",
    "score_on_split": "experiment",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Resolve a public name to its module on first access."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value  # cache, so repeated access is a plain lookup
    return value


def __dir__() -> list[str]:
    return __all__
