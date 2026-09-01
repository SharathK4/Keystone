"""Turning benchmark scenarios into supervised examples.

One example is one ``(dataset, scenario)`` pair observed at its prediction
origin. The observable half comes from :mod:`lce.learning.problem`; the labels
come from :class:`~lce.benchmark.ground_truth.ScenarioGroundTruth` and are read,
never redefined - the attributable affected set and the first-constraint times
are Phase-2 definitions and stay frozen.

Two label conventions worth stating explicitly, because both change what the
numbers mean:

**The universe keeps its background failures.** Merchants that fail in the
no-shock baseline stay in the scored universe as negatives. They are not
shock-affected, so flagging one is a false positive and should cost the model.
Dropping them would quietly inflate precision.

**Nodes already constrained at the origin are excluded.** For the mutation
families the perturbed book exists from ``t = 0``, so a merchant can be broken
before the nominal onset. Asking a model to *forecast* that is asking it to
predict the past; those nodes leave the universe and are counted separately.

Censoring
---------
Observation stops at the horizon. A merchant never constrained inside it is a
complete observation of survival through every interval, not a node labelled
"safe forever" - it contributes to the survival likelihood through
``P(tau > T - t0)`` and contributes nothing to the timing error. Positives whose
constraint time was never recorded are the mirror image: real events, unusable
timestamps, so they count for classification and are dropped from timing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from lce.benchmark.ground_truth import ScenarioGroundTruth, compute_ground_truth
from lce.benchmark.scales import BenchmarkScale, scale_config
from lce.benchmark.scenarios import (
    BuiltScenario,
    ScenarioFamily,
    baseline_affected_set,
    scenario_suite,
)
from lce.data.generator import GeneratorConfig, SyntheticNetwork, generate_network
from lce.domain.edges import DependencyEdge
from lce.learning.features import (
    NODE_FEATURE_NAMES,
    ObservedStats,
    build_interval_features,
    build_node_features,
    build_pair_features,
)
from lce.learning.problem import (
    DEFAULT_OBSERVATION,
    DEFAULT_TASK,
    LeakageAudit,
    ObservationSpec,
    ObservedWindow,
    PredictionTask,
    audit_leakage,
    audit_window,
    baseline_payment_stream,
    build_observed_window,
)
from lce.logging import get_logger
from lce.simulation.engine import SimulationConfig

logger = get_logger(__name__)

CORPUS_FORMAT_VERSION = 1


@dataclass(slots=True)
class ContagionExample:
    """One scenario, observed at its origin, with its labels attached."""

    dataset_id: str
    scenario_id: str
    family: str
    seed: int
    epoch: float
    origin_t: float
    horizon_end: float

    merchant_ids: list[str]
    x: np.ndarray
    interval_x: np.ndarray
    interval_edges: np.ndarray

    y: np.ndarray
    """1 where the merchant becomes attributably constrained inside the window."""
    tau: np.ndarray
    """Hours from origin to first constraint; the censoring time where unobserved."""
    timing_observed: np.ndarray
    """1 where ``tau`` is a recorded first-constraint time rather than an imputation
    or the administrative horizon. Timing metrics are scored only on these."""
    in_universe: np.ndarray
    """0 for merchants already constrained at the origin - excluded from scoring."""
    shock_origin: np.ndarray
    """1 for the merchants the shock hit directly.

    Observable - it is the operator's own trigger - but recorded separately so
    evaluation can report the **downstream** view. Directly-shocked nodes usually
    fail, and they are trivially identifiable, so leaving them in the pooled
    metric lets a model score highly by repeating back the input. Contagion
    prediction is the downstream number."""

    pair_keys: list[tuple[str, str]]
    pair_x: np.ndarray

    window: ObservedWindow | None = field(default=None, repr=False)
    """Retained so the point-process and graph models can re-read the raw stream.
    Dropped when a corpus is written to disk."""

    @property
    def absolute_origin(self) -> float:
        """Origin on the corpus-wide clock - the axis the temporal split cuts on."""
        return self.epoch + self.origin_t

    @property
    def remaining_hours(self) -> float:
        return max(self.horizon_end - self.origin_t, 1e-6)

    @property
    def n_merchants(self) -> int:
        return len(self.merchant_ids)

    def labels_at(self, t: float) -> np.ndarray:
        """Binary label for "constrained within ``t`` hours of the origin"."""
        return ((self.y > 0) & (self.tau <= t + 1e-9)).astype(np.float64)

    def universe_mask(self) -> np.ndarray:
        return self.in_universe > 0

    def downstream_mask(self) -> np.ndarray:
        """Scored universe minus the directly-shocked nodes - true contagion."""
        return self.universe_mask() & (self.shock_origin <= 0)

    def summary(self) -> dict[str, Any]:
        mask = self.universe_mask()
        return {
            "dataset_id": self.dataset_id,
            "scenario_id": self.scenario_id,
            "family": self.family,
            "origin_t": round(self.origin_t, 2),
            "absolute_origin": round(self.absolute_origin, 2),
            "remaining_hours": round(self.remaining_hours, 2),
            "n_merchants": self.n_merchants,
            "n_in_universe": int(mask.sum()),
            "n_positive": int(self.y[mask].sum()),
            "positive_rate": float(self.y[mask].mean()) if mask.any() else 0.0,
            "n_shock_origins": int(self.shock_origin.sum()),
            "n_positive_downstream": int(self.y[self.downstream_mask()].sum()),
        }


@dataclass(slots=True)
class HiddenTruth:
    """Latent quantities held for scoring only - never handed to a model.

    Kept on the corpus rather than on the example so there is no path from an
    example object to the answer. A model receives examples.
    """

    true_edges: dict[str, dict[tuple[str, str], DependencyEdge]] = field(default_factory=dict)
    ground_truth: dict[str, ScenarioGroundTruth] = field(default_factory=dict)

    def edges_for(self, dataset_id: str) -> dict[tuple[str, str], DependencyEdge]:
        return self.true_edges.get(dataset_id, {})


@dataclass(slots=True)
class ExampleCorpus:
    """Every example built for an experiment, plus the truth kept behind the wall."""

    examples: list[ContagionExample] = field(default_factory=list)
    hidden: HiddenTruth = field(default_factory=HiddenTruth)
    observation: ObservationSpec = DEFAULT_OBSERVATION
    task: PredictionTask = DEFAULT_TASK
    datasets: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.examples)

    def dataset_ids(self) -> list[str]:
        return sorted({e.dataset_id for e in self.examples})

    def families(self) -> list[str]:
        return sorted({e.family for e in self.examples})

    def summary(self) -> dict[str, Any]:
        positives = sum(int(e.y[e.universe_mask()].sum()) for e in self.examples)
        universe = sum(int(e.universe_mask().sum()) for e in self.examples)
        downstream = sum(int(e.y[e.downstream_mask()].sum()) for e in self.examples)
        downstream_universe = sum(int(e.downstream_mask().sum()) for e in self.examples)
        return {
            "n_examples": len(self.examples),
            "n_datasets": len(self.dataset_ids()),
            "families": self.families(),
            "n_scored_nodes": universe,
            "n_positive": positives,
            "positive_rate": positives / universe if universe else 0.0,
            "n_positive_downstream": downstream,
            "downstream_positive_rate": (
                downstream / downstream_universe if downstream_universe else 0.0
            ),
            "observation": self.observation.to_dict(),
            "task": self.task.to_dict(),
            "node_feature_names": list(NODE_FEATURE_NAMES),
        }


def _labels_from_truth(
    truth: ScenarioGroundTruth,
    merchant_ids: Sequence[str],
    origin: float,
    remaining: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``(y, tau, timing_observed, in_universe)`` for one scenario.

    The survival convention is administrative censoring at the horizon: a
    positive is an *event* at ``tau``; a negative is a complete observation of
    survival through all ``K`` intervals. ``tau`` therefore holds the recorded
    first-constraint time for hits and the full remaining horizon otherwise.

    One awkward case is handled explicitly. A merchant can be attributably
    affected - it missed an obligation - without ever tripping the constrained
    flag, so the event is real but its time was never recorded. Those are
    labelled positive with ``tau`` at the end of the window, which keeps the
    survival target consistent with the classification label, and
    ``timing_observed = 0``, which keeps them out of every timing metric rather
    than scoring a model against an invented timestamp.
    """
    affected = set(truth.affected_nodes)
    hit_times = truth.first_constraint_t

    n = len(merchant_ids)
    y = np.zeros(n, dtype=np.float64)
    tau = np.full(n, float(remaining), dtype=np.float64)
    timing_observed = np.zeros(n, dtype=np.float64)
    in_universe = np.ones(n, dtype=np.float64)

    for i, merchant_id in enumerate(merchant_ids):
        if merchant_id not in affected:
            continue
        hit = hit_times.get(merchant_id)
        if hit is None:
            # Event real, time unrecorded: positive, timing excluded.
            y[i] = 1.0
            continue
        if hit <= origin:
            # Already broken before the origin - not a forecast.
            in_universe[i] = 0.0
            continue
        y[i] = 1.0
        tau[i] = float(min(hit - origin, remaining))
        timing_observed[i] = 1.0
    return y, tau, timing_observed, in_universe


def build_example(
    scenario: BuiltScenario,
    truth: ScenarioGroundTruth,
    *,
    config: SimulationConfig,
    baseline_payments: Sequence[Any],
    seed: int,
    epoch: float,
    observation: ObservationSpec = DEFAULT_OBSERVATION,
    task: PredictionTask = DEFAULT_TASK,
    keep_window: bool = True,
    graph_cache: dict[Any, Any] | None = None,
    audit: LeakageAudit | None = None,
) -> ContagionExample:
    """Assemble one example from a built scenario and its ground truth.

    When ``audit`` is supplied it is filled in by :func:`~lce.learning.problem.audit_window`,
    which is the only place the check can be made: it compares the window against
    the scenario's *unperturbed* obligation book, and the scenario is deliberately
    not retained on the example afterwards.
    """
    window = build_observed_window(
        scenario,
        config=config,
        baseline_payments=baseline_payments,
        spec=observation,
        graph_cache=graph_cache,
    )
    if audit is not None:
        audit.probes.update(audit_window(window, scenario).probes)

    stats = ObservedStats.build(window)
    ids, x = build_node_features(window, stats)
    edges = task.interval_edges(window.remaining_hours)
    interval_x = build_interval_features(window, edges, stats)
    pair_keys, pair_x = build_pair_features(window, stats)

    y, tau, timing_observed, in_universe = _labels_from_truth(
        truth, ids, window.origin_t, window.remaining_hours
    )
    origins = set(scenario.shock.origin_ids)
    shock_origin = np.array([1.0 if m in origins else 0.0 for m in ids], dtype=np.float64)

    return ContagionExample(
        dataset_id=scenario.dataset_id,
        scenario_id=scenario.scenario_id,
        family=str(scenario.spec.family),
        seed=seed,
        epoch=epoch,
        origin_t=window.origin_t,
        horizon_end=window.horizon_end,
        merchant_ids=ids,
        x=x,
        interval_x=interval_x,
        interval_edges=edges,
        y=y,
        tau=tau,
        timing_observed=timing_observed,
        in_universe=in_universe,
        shock_origin=shock_origin,
        pair_keys=pair_keys,
        pair_x=pair_x,
        window=window if keep_window else None,
    )


def build_dataset_examples(
    *,
    seed: int,
    epoch: float,
    scale: BenchmarkScale | str = BenchmarkScale.SMALL,
    generator: GeneratorConfig | None = None,
    families: tuple[ScenarioFamily, ...] | None = None,
    magnitude: float = 2.0,
    observation: ObservationSpec = DEFAULT_OBSERVATION,
    task: PredictionTask = DEFAULT_TASK,
    keep_window: bool = True,
    audit: bool = True,
) -> tuple[
    list[ContagionExample],
    SyntheticNetwork,
    dict[str, ScenarioGroundTruth],
    dict[str, Any],
]:
    """Generate one benchmark dataset and turn its scenario suite into examples.

    The no-shock baseline is simulated **once** and shared: it is the same world
    for every scenario on this network, and it supplies both the pre-origin
    observable payment stream and the reference the labels are differenced
    against.
    """
    config = generator or scale_config(scale, seed=seed)
    network = generate_network(config)
    sim = SimulationConfig(horizon_hours=config.horizon_hours, seed=seed)

    already_failing = baseline_affected_set(network.graph, sim)
    suite = scenario_suite(
        network.graph,
        dataset_id=network.dataset_version,
        seed=seed,
        magnitude=magnitude,
        families=families,
        config=sim,
        baseline_affected=already_failing,
    )
    baseline_payments = baseline_payment_stream(network.graph, sim)

    examples: list[ContagionExample] = []
    truths: dict[str, ScenarioGroundTruth] = {}
    graph_cache: dict[Any, Any] = {}
    audits: dict[str, Any] = {"windows": {}, "perturbation": {}}
    for position, scenario in enumerate(suite):
        truth = compute_ground_truth(
            scenario,
            true_edges=network.ground_truth_edges,
            config=sim,
            compute_optimum=False,
        )
        truths[scenario.scenario_id] = truth
        window_audit = LeakageAudit(scenario_id=scenario.scenario_id) if audit else None
        examples.append(
            build_example(
                scenario,
                truth,
                config=sim,
                baseline_payments=baseline_payments,
                seed=seed,
                epoch=epoch,
                observation=observation,
                task=task,
                keep_window=keep_window,
                graph_cache=graph_cache,
                audit=window_audit,
            )
        )
        if window_audit is not None:
            audits["windows"][scenario.scenario_id] = window_audit.to_dict()
            if position == 0:
                # One perturbation probe per dataset. It rebuilds the window under
                # three counterfactuals, so running it on every scenario would
                # triple the build; one per network is enough to catch a
                # regression in the barrier, which is what it guards.
                audits["perturbation"][scenario.scenario_id] = audit_leakage(
                    lambda w: build_node_features(w)[1],
                    scenario,
                    config=sim,
                    baseline_payments=baseline_payments,
                    spec=observation,
                    raise_on_failure=True,
                ).to_dict()

    logger.info(
        "dataset_examples_built",
        dataset_id=network.dataset_version,
        seed=seed,
        n_examples=len(examples),
        n_merchants=len(network.graph),
    )
    return examples, network, truths, audits


def build_corpus(
    seeds: Sequence[int],
    *,
    scale: BenchmarkScale | str = BenchmarkScale.SMALL,
    overrides: dict[str, Any] | None = None,
    families: tuple[ScenarioFamily, ...] | None = None,
    magnitude: float = 2.0,
    observation: ObservationSpec = DEFAULT_OBSERVATION,
    task: PredictionTask = DEFAULT_TASK,
    keep_window: bool = True,
    audit: bool = True,
) -> ExampleCorpus:
    """Build the full corpus over a sequence of dataset seeds.

    ``overrides`` are generator parameters applied on top of the scale profile -
    used by the tests to build networks small enough to run in seconds, and
    available for sensitivity work.

    Each dataset is stamped with an epoch ``rank * (H + T)`` so every example has
    a position on a single clock. Within a dataset the ordering is real time;
    across datasets it is a protocol convention, and it is the convention that
    makes the temporal split enforceable - see ``docs/PHASE3_DESIGN.md``.
    """
    corpus = ExampleCorpus(observation=observation, task=task)
    for rank, seed in enumerate(seeds):
        config = scale_config(scale, seed=seed, overrides=overrides)
        epoch = rank * (config.history_hours + config.horizon_hours)
        examples, network, truths, audits = build_dataset_examples(
            seed=seed,
            epoch=epoch,
            scale=scale,
            generator=config,
            families=families,
            magnitude=magnitude,
            observation=observation,
            task=task,
            keep_window=keep_window,
            audit=audit,
        )
        corpus.examples.extend(examples)
        corpus.hidden.true_edges[network.dataset_version] = network.ground_truth_edges
        corpus.hidden.ground_truth.update(truths)
        corpus.datasets[network.dataset_version] = {
            "seed": seed,
            "rank": rank,
            "epoch": epoch,
            "scale": str(scale),
            "n_merchants": len(network.graph),
            "n_events": network.graph.stats().n_payment_events,
            "leakage_audit": audits,
        }
    logger.info("corpus_built", **corpus.summary())
    return corpus


# ---------------------------------------------------------------- persistence


def save_corpus(corpus: ExampleCorpus, directory: Path) -> Path:
    """Write the observable half of a corpus to disk.

    Only the observable half: the hidden truth is deliberately not serialised
    here. A corpus on disk is something a model may read, so writing the answers
    next to the questions would defeat the point of having a barrier at all.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    manifest = {
        "format_version": CORPUS_FORMAT_VERSION,
        "summary": corpus.summary(),
        "datasets": corpus.datasets,
        "examples": [e.summary() for e in corpus.examples],
    }
    (directory / "corpus.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )

    payload: dict[str, np.ndarray] = {}
    for i, example in enumerate(corpus.examples):
        payload[f"x_{i}"] = example.x
        payload[f"interval_x_{i}"] = example.interval_x
        payload[f"interval_edges_{i}"] = example.interval_edges
        payload[f"y_{i}"] = example.y
        payload[f"tau_{i}"] = example.tau
        payload[f"timing_observed_{i}"] = example.timing_observed
        payload[f"in_universe_{i}"] = example.in_universe
        payload[f"shock_origin_{i}"] = example.shock_origin
        payload[f"pair_x_{i}"] = example.pair_x
        payload[f"merchant_ids_{i}"] = np.array(example.merchant_ids, dtype=object)
        payload[f"pair_keys_{i}"] = np.array(
            [f"{s}>{t}" for s, t in example.pair_keys], dtype=object
        )
        payload[f"meta_{i}"] = np.array(
            [
                example.dataset_id,
                example.scenario_id,
                example.family,
                str(example.seed),
                str(example.epoch),
                str(example.origin_t),
                str(example.horizon_end),
            ],
            dtype=object,
        )
    np.savez_compressed(str(directory / "examples.npz"), **payload)  # type: ignore[arg-type]
    return directory


def _split_pair(encoded: str) -> tuple[str, str]:
    source, _, target = encoded.partition(">")
    return (source, target)


def load_corpus(directory: Path) -> ExampleCorpus:
    """Read back a corpus written by :func:`save_corpus`.

    The reloaded examples have no ``window``: the models that need the raw event
    stream (the point-process and graph models) must be run against a freshly
    built corpus, since the stream is not part of the persisted feature tables.
    """
    directory = Path(directory)
    manifest = json.loads((directory / "corpus.json").read_text(encoding="utf-8"))
    if manifest.get("format_version") != CORPUS_FORMAT_VERSION:
        raise ValueError(
            f"unsupported corpus format {manifest.get('format_version')!r}; "
            f"expected {CORPUS_FORMAT_VERSION}"
        )

    archive = np.load(directory / "examples.npz", allow_pickle=True)
    corpus = ExampleCorpus(datasets=manifest.get("datasets", {}))
    for i in range(len(manifest["examples"])):
        meta = [str(v) for v in archive[f"meta_{i}"]]
        corpus.examples.append(
            ContagionExample(
                dataset_id=meta[0],
                scenario_id=meta[1],
                family=meta[2],
                seed=int(meta[3]),
                epoch=float(meta[4]),
                origin_t=float(meta[5]),
                horizon_end=float(meta[6]),
                merchant_ids=[str(m) for m in archive[f"merchant_ids_{i}"]],
                x=archive[f"x_{i}"],
                interval_x=archive[f"interval_x_{i}"],
                interval_edges=archive[f"interval_edges_{i}"],
                y=archive[f"y_{i}"],
                tau=archive[f"tau_{i}"],
                timing_observed=archive[f"timing_observed_{i}"],
                in_universe=archive[f"in_universe_{i}"],
                shock_origin=archive[f"shock_origin_{i}"],
                pair_keys=[_split_pair(str(k)) for k in archive[f"pair_keys_{i}"]],
                pair_x=archive[f"pair_x_{i}"],
                window=None,
            )
        )
    return corpus
