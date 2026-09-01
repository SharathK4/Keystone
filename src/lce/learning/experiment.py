"""The Phase-3 experiment: build, split, fit, calibrate, score.

One entry point, :func:`run_phase3`, executes the whole protocol in the order the
protocol requires and refuses to proceed if any of its preconditions fail:

1. **Build** the corpus of ``(dataset, scenario)`` examples.
2. **Audit** the observable windows - directly, and by perturbation - so a leak is
   an exception rather than a footnote.
3. **Split** in origin-time order and *verify* the split's guarantees.
4. **Fit** every model on train only.
5. **Calibrate** on validation only, and pick the decision threshold there too.
6. **Score** on test, once.
7. **Recover** the latent dependency structure, unsupervised, and separately
   report the supervised upper bound.

The ordering is not stylistic. Calibrating or thresholding anywhere other than
validation, or looking at test more than once, produces numbers that cannot be
reproduced on data the model has not seen - which is the failure this whole phase
is built to avoid.

Determinism
-----------
Everything is a function of ``Phase3Config``: the dataset seeds, the observation
spec, the task discretisation, the split fractions and the model seeds. The
config hashes into ``config_hash``, and that hash plus the seeds is what a result
should be quoted with.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from lce.benchmark.scales import BenchmarkScale
from lce.errors import ModelError
from lce.learning.baselines import (
    CashCoverBaseline,
    ContagionModel,
    DiscreteTimeHazard,
    PrevalenceBaseline,
    ShockDistanceBaseline,
)
from lce.learning.calibration import Calibrator, select_calibrator
from lce.learning.dataset import ExampleCorpus, build_corpus
from lce.learning.evaluation import (
    LearningReport,
    ScoreCard,
    best_f1_threshold,
    evaluate_forecasts,
    pooled,
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
    ObservationSpec,
    PredictionTask,
)
from lce.learning.splits import SplitSpec, assert_split_clean, make_temporal_split
from lce.logging import get_logger
from lce.seeds import config_hash

logger = get_logger(__name__)

#: Model keys the experiment knows how to build. ``temporal_gnn`` needs the
#: optional ``ml`` extra and is skipped with a recorded warning when absent.
MODEL_KEYS: tuple[str, ...] = (
    "prevalence",
    "cash_cover",
    "shock_distance",
    "discrete_hazard",
    "hawkes_linear",
    "hawkes_cascade",
    "temporal_gnn",
)


@dataclass(frozen=True, slots=True)
class Phase3Config:
    """Everything that determines a Phase-3 result."""

    seeds: tuple[int, ...] = tuple(range(101, 125))
    scale: str = str(BenchmarkScale.SMALL)
    magnitude: float = 2.0
    observation: ObservationSpec = DEFAULT_OBSERVATION
    task: PredictionTask = DEFAULT_TASK
    split: SplitSpec = field(default_factory=SplitSpec)
    models: tuple[str, ...] = MODEL_KEYS
    seed: int = 20250101

    def to_dict(self) -> dict[str, Any]:
        return {
            "seeds": list(self.seeds),
            "scale": self.scale,
            "magnitude": self.magnitude,
            "observation": self.observation.to_dict(),
            "task": self.task.to_dict(),
            "split": self.split.to_dict(),
            "models": list(self.models),
            "seed": self.seed,
        }

    @property
    def config_hash(self) -> str:
        return config_hash(self.to_dict())


def build_models(
    config: Phase3Config,
    *,
    estimator: HawkesDependencyEstimator | None = None,
) -> dict[str, ContagionModel]:
    """Instantiate the requested models, sharing one dependency estimator.

    Sharing matters for cost, not for correctness: the marked-Hawkes EM is by far
    the most expensive step, its result depends only on the observed window, and
    both point-process variants and the graph model want the same fit.
    """
    estimator = estimator or HawkesDependencyEstimator()
    task = config.task
    built: dict[str, ContagionModel] = {}

    for key in config.models:
        if key == "prevalence":
            built[key] = PrevalenceBaseline(task)
        elif key == "cash_cover":
            built[key] = CashCoverBaseline(task)
        elif key == "shock_distance":
            built[key] = ShockDistanceBaseline(task)
        elif key == "discrete_hazard":
            built[key] = DiscreteTimeHazard(task)
        elif key == "hawkes_linear":
            built[key] = HawkesContagionModel(
                task, propagator="linear_threshold", estimator=estimator
            )
        elif key == "hawkes_cascade":
            built[key] = HawkesContagionModel(
                task, propagator="hawkes_cascade", estimator=estimator
            )
        elif key == "temporal_gnn":
            model = _maybe_graph_model(config, estimator)
            if model is not None:
                built[key] = model
        else:
            raise ModelError(f"unknown model key {key!r}; expected one of {MODEL_KEYS}")
    return built


def _maybe_graph_model(
    config: Phase3Config, estimator: HawkesDependencyEstimator
) -> ContagionModel | None:
    """Build the graph model, or return ``None`` if torch is not installed."""
    try:
        from lce.learning.graphmodel import TemporalGraphModel
        from lce.models.tgnn import TGNNConfig
    except ImportError:  # pragma: no cover - depends on install extras
        return None
    try:
        return TemporalGraphModel(
            config.task,
            config=TGNNConfig(seed=config.seed),
            estimator=estimator,
        )
    except Exception as exc:  # pragma: no cover - torch missing at construction
        logger.warning("temporal_gnn_unavailable", error=str(exc))
        return None


@dataclass(slots=True)
class FittedModel:
    """A fitted model with the calibrator and threshold chosen on validation."""

    key: str
    model: ContagionModel
    calibrator: Calibrator
    threshold: float
    calibrator_scores: dict[str, float] = field(default_factory=dict)
    fit_report: dict[str, Any] = field(default_factory=dict)


def fit_and_calibrate(
    key: str,
    model: ContagionModel,
    train: Sequence[Any],
    validation: Sequence[Any],
) -> FittedModel:
    """Fit on train, then choose the calibrator and threshold on validation.

    Both choices are made on validation and then frozen. Selecting either on test
    would turn a held-out measurement into a second fit, and on a base rate this
    low the threshold in particular is worth several points of F1.
    """
    report = model.fit(train, validation)
    validation_scores, validation_labels = pooled(validation, model.predict_all(validation))
    calibrator, scores = select_calibrator(validation_scores, validation_labels)
    threshold, _ = best_f1_threshold(
        calibrator.transform(validation_scores), validation_labels
    )
    return FittedModel(
        key=key,
        model=model,
        calibrator=calibrator,
        threshold=threshold,
        calibrator_scores=scores,
        fit_report=report,
    )


def audit_corpus(corpus: ExampleCorpus, config: Phase3Config) -> dict[str, Any]:
    """Collect the leakage evidence before anything is fitted.

    Two sources. The corpus builder ran :func:`~lce.learning.problem.audit_window`
    on every scenario while the scenario object was still in scope - that is the
    only moment the *unperturbed book* probe can be made, since the example does
    not retain the scenario - and one perturbation probe per dataset. Here those
    recorded results are collated, and the windows that survived in memory are
    re-opened for the two checks that need no scenario.

    A failure here stops the run. A model fitted on a contaminated corpus reports
    numbers that will not reproduce on real data, which is strictly worse than
    reporting none.
    """
    failures: list[str] = []
    n_window_probes = 0
    n_perturbation_probes = 0

    for dataset_id, meta in corpus.datasets.items():
        recorded = meta.get("leakage_audit") or {}
        for scenario_id, result in (recorded.get("windows") or {}).items():
            n_window_probes += 1
            if not result.get("clean", True):
                failures.append(
                    f"{dataset_id}/{scenario_id}: {', '.join(result.get('failures', []))}"
                )
        for scenario_id, result in (recorded.get("perturbation") or {}).items():
            n_perturbation_probes += 1
            if not result.get("clean", True):
                failures.append(
                    f"{dataset_id}/{scenario_id}: perturbation probe "
                    f"{', '.join(result.get('failures', []))}"
                )

    n_reopened = 0
    for example in corpus.examples:
        if example.window is None:
            continue
        n_reopened += 1
        graph = example.window.graph
        if any(e.t >= example.window.origin_t for e in graph.payment_events):
            failures.append(f"{example.scenario_id}: window holds post-origin events")
        if graph.dependency_edges:
            failures.append(f"{example.scenario_id}: window holds dependency edges")

    return {
        "clean": not failures,
        "window_probes": n_window_probes,
        "perturbation_probes": n_perturbation_probes,
        "windows_reopened": n_reopened,
        "failures": failures,
        "observation": config.observation.to_dict(),
    }


def run_phase3(
    config: Phase3Config = Phase3Config(),
    *,
    corpus: ExampleCorpus | None = None,
    run_ablations: bool = False,
    full_ablations: bool = False,
) -> LearningReport:
    """Execute the whole protocol and return the report."""
    started = time.perf_counter()

    corpus = corpus or build_corpus(
        config.seeds,
        scale=config.scale,
        magnitude=config.magnitude,
        observation=config.observation,
        task=config.task,
    )
    audit = audit_corpus(corpus, config)
    if not audit["clean"]:
        raise ModelError(
            f"leakage audit failed before fitting: {audit['failures'][:5]}"
        )

    split = make_temporal_split(corpus, config.split)
    split_audit = assert_split_clean(corpus, split)

    train = split.examples(corpus, "train")
    validation = split.examples(corpus, "validation")
    test = split.examples(corpus, "test")

    estimator = HawkesDependencyEstimator()
    models = build_models(config, estimator=estimator)

    report = LearningReport()
    report.corpus = corpus.summary() | {
        "config_hash": config.config_hash,
        "config": config.to_dict(),
        "n_train": len(train),
        "n_validation": len(validation),
        "n_test": len(test),
    }
    report.split_audit = split_audit.to_dict() | {
        "split": split.to_dict(),
        "window_audit": audit,
    }

    fitted: dict[str, FittedModel] = {}
    for key, model in models.items():
        elapsed = time.perf_counter()
        fitted[key] = fit_and_calibrate(key, model, train, validation)
        for split_name, examples in (("validation", validation), ("test", test)):
            report.cards.append(
                score_on_split(fitted[key], examples, split_name, config.task)
            )
        logger.info(
            "phase3_model_done", model=key, seconds=round(time.perf_counter() - elapsed, 1)
        )

    report.dependency = _dependency_section(corpus, train, test, estimator)
    if run_ablations:
        from lce.learning.ablations import run_ablation_suite

        report.ablations = run_ablation_suite(
            config,
            corpus,
            split,
            estimator=estimator,
            reference=fitted,
            full=full_ablations,
        )

    report.elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "phase3_complete",
        n_models=len(models),
        n_test=len(test),
        elapsed_s=round(report.elapsed_ms / 1000.0, 1),
    )
    return report


def score_on_split(
    fitted: FittedModel,
    examples: Sequence[Any],
    split_name: str,
    task: PredictionTask,
    *,
    n_bootstrap: int = 1000,
) -> ScoreCard:
    """Score a fitted model on one block, applying its frozen calibrator."""
    card = evaluate_forecasts(
        examples,
        fitted.model.predict_all(examples),
        model=fitted.key,
        split=split_name,
        task=task,
        calibrator=fitted.calibrator,
        threshold=fitted.threshold,
        n_bootstrap=n_bootstrap,
    )
    card.calibrator = dict(fitted.calibrator.to_dict())
    card.calibrator["selection_log_loss"] = dict(fitted.calibrator_scores)
    return card


def _dependency_section(
    corpus: ExampleCorpus,
    train: Sequence[Any],
    test: Sequence[Any],
    estimator: HawkesDependencyEstimator,
) -> dict[str, Any]:
    """Task 3: unsupervised recovery, with the supervised upper bound alongside.

    The two are reported together and labelled, never merged. The unsupervised
    estimator is the result; the supervised regression is a reference that trains
    on hidden labels and therefore cannot be a result at all.
    """
    recovery = evaluate_dependency_recovery(corpus, test, estimator=estimator)
    try:
        supervised = SupervisedDependencyRegressor()
        supervised.fit(corpus, train)
        recovery.supervised = supervised.score(corpus, test)
    except ModelError as exc:  # pragma: no cover - only when no pairs match
        recovery.supervised = {"error": str(exc)}
    return recovery.to_dict()


def quick_config(seeds: Sequence[int] | None = None, **overrides: Any) -> Phase3Config:
    """A small, fast configuration - used by the tests and the smoke CLI."""
    base = Phase3Config(
        seeds=tuple(seeds if seeds is not None else range(201, 207)),
        models=("prevalence", "cash_cover", "shock_distance", "discrete_hazard"),
        task=PredictionTask(horizon_grid=(24.0, 72.0), n_hazard_intervals=6),
    )
    return replace(base, **overrides) if overrides else base
