"""Ablations: which part of the system is actually doing the work.

A leaderboard says which model won. It does not say *why*, and on a benchmark
where the directly-shocked merchant is both trivially identifiable and usually a
victim, "why" is the whole question. Each ablation here removes exactly one thing
and re-runs the protocol unchanged - same split, same calibration discipline,
same test block, scored once.

The suite
---------
``no_graph``
    the hazard model with every column that needed a counterparty identity
    removed (see :data:`~lce.learning.features.NETWORK_DEPENDENT_COLUMNS`). What
    survives is what a merchant could compute from its own bank statement. The
    gap is what the network is worth.

``true_structure``
    the graph model handed the generator's real dependency overlay instead of the
    estimated one. **Leaky by construction** - it is an upper bound on what
    perfect structure recovery would buy, and is reported as such, never as a
    result. The gap between it and ``temporal_gnn`` is dependency-estimation
    error; the gap between it and the ceiling is everything else.

``shuffled_edges``
    the estimated overlay with its endpoints permuted. Degrees and edge-attribute
    marginals survive, topology does not. A model that scores as well here as with
    the real structure was never using the structure - which is the failure mode
    graph models most often hide.

``no_edges``
    the same trunk with no edges at all, which collapses it to an MLP on the node
    features. Separates "graph neural network" from "neural network".

``no_balance_sheet``
    the one arguable disclosure in the observation spec, removed. Needs a corpus
    rebuild, since it changes what the features are computed from rather than
    which of them are used.

``no_shock_descriptor``
    the operator's trigger removed: the model must find the origin itself. Also
    needs a rebuild, and the point-process models cannot run at all without it,
    so it is scored on the hazard model.

Reading the results
-------------------
Deltas are reported against the corresponding un-ablated model on the same test
block, in both the pooled and the downstream view. The downstream delta is the
informative one: pooled numbers are dominated by the shocked nodes, which no
ablation here affects.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from lce.learning.baselines import DiscreteTimeHazard
from lce.learning.dataset import ContagionExample, ExampleCorpus, build_corpus
from lce.learning.evaluation import ScoreCard
from lce.learning.features import network_free_mask
from lce.learning.pointprocess import HawkesDependencyEstimator
from lce.learning.problem import ObservationSpec
from lce.learning.splits import TemporalSplit, assert_split_clean, make_temporal_split
from lce.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class AblationResult:
    """One ablation, scored on test against its un-ablated reference."""

    name: str
    question: str
    reference: str
    leaky: bool = False
    card: dict[str, Any] = field(default_factory=dict)
    delta: dict[str, float | None] = field(default_factory=dict)
    note: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "question": self.question,
            "reference": self.reference,
            "leaky": self.leaky,
            "card": self.card,
            "delta": self.delta,
            "note": self.note,
            "elapsed_s": round(self.elapsed_s, 1),
        }


def _delta(card: ScoreCard, reference: ScoreCard | None) -> dict[str, float | None]:
    if reference is None:
        return {}

    def gap(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else float(a - b)

    return {
        "pr_auc": gap(card.pr_auc, reference.pr_auc),
        "downstream_pr_auc": gap(
            card.downstream.get("pr_auc"), reference.downstream.get("pr_auc")
        ),
        "roc_auc": gap(card.roc_auc, reference.roc_auc),
        "brier": gap(card.calibration.get("brier"), reference.calibration.get("brier")),
        "timing_mae_hours": gap(card.timing.mae_hours, reference.timing.mae_hours),
    }


def _run_one(
    key: str,
    model: Any,
    train: Sequence[ContagionExample],
    validation: Sequence[ContagionExample],
    test: Sequence[ContagionExample],
    task: Any,
) -> ScoreCard:
    from lce.learning.experiment import fit_and_calibrate, score_on_split

    # No bootstrap here: an ablation is read as a *delta* against its reference
    # on the same block, and resampling every variant would triple the sweep for
    # an interval the delta already carries.
    return score_on_split(
        fit_and_calibrate(key, model, train, validation),
        test,
        "test",
        task,
        n_bootstrap=0,
    )


def run_ablation_suite(
    config: Any,
    corpus: ExampleCorpus,
    split: TemporalSplit,
    *,
    estimator: HawkesDependencyEstimator | None = None,
    reference: dict[str, Any] | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Run every ablation and return the results keyed by name.

    ``reference`` is the mapping of fitted models from the main run, used to
    recover each ablation's baseline scorecard. ``full`` additionally runs the two
    ablations that need the corpus rebuilt, which roughly triples the runtime.
    """
    from lce.learning.experiment import score_on_split

    estimator = estimator or HawkesDependencyEstimator()
    train = split.examples(corpus, "train")
    validation = split.examples(corpus, "validation")
    test = split.examples(corpus, "test")

    baselines: dict[str, ScoreCard] = {}
    for key, fitted in (reference or {}).items():
        baselines[key] = score_on_split(
            fitted, test, "test", config.task, n_bootstrap=0
        )

    results: list[AblationResult] = []

    # --- no_graph -----------------------------------------------------------
    started = time.perf_counter()
    mask = network_free_mask()
    card = _run_one(
        "no_graph",
        DiscreteTimeHazard(config.task, feature_mask=mask),
        train,
        validation,
        test,
        config.task,
    )
    results.append(
        AblationResult(
            name="no_graph",
            question="is the network worth anything over per-merchant features?",
            reference="discrete_hazard",
            card=card.headline(),
            delta=_delta(card, baselines.get("discrete_hazard")),
            note=f"{int(mask.sum())} of {mask.size} node columns retained",
            elapsed_s=time.perf_counter() - started,
        )
    )

    # --- graph-structure variants -------------------------------------------
    results.extend(
        _structure_ablations(
            config, corpus, train, validation, test, estimator, baselines
        )
    )

    # --- observation-level ablations ----------------------------------------
    if full:
        results.extend(_observation_ablations(config, baselines))

    return {
        "results": [r.to_dict() for r in results],
        "full": full,
        "note": (
            "Deltas are against the same model without the ablation, on the same "
            "test block. Entries marked leaky train or condition on hidden "
            "variables and are upper bounds, not results."
        ),
    }


def _structure_ablations(
    config: Any,
    corpus: ExampleCorpus,
    train: Sequence[ContagionExample],
    validation: Sequence[ContagionExample],
    test: Sequence[ContagionExample],
    estimator: HawkesDependencyEstimator,
    baselines: dict[str, ScoreCard],
) -> list[AblationResult]:
    """Graph-model variants that differ only in which structure they are given."""
    try:
        from lce.learning.graphmodel import GraphSampleSpec, TemporalGraphModel
        from lce.models.tgnn import TGNNConfig
    except ImportError:  # pragma: no cover - depends on install extras
        return [
            AblationResult(
                name="structure_variants",
                question="how much does the estimated structure contribute?",
                reference="temporal_gnn",
                note="skipped: the temporal graph model needs the 'ml' extra",
            )
        ]

    specs = (
        (
            "true_structure",
            "true",
            "how much of the gap is dependency-estimation error?",
            True,
        ),
        (
            "shuffled_edges",
            "shuffled",
            "negative control: same degrees, destroyed topology",
            False,
        ),
        ("no_edges", "none", "graph neural network, or just neural network?", False),
    )

    results: list[AblationResult] = []
    for name, structure, question, leaky in specs:
        started = time.perf_counter()
        model = TemporalGraphModel(
            config.task,
            config=TGNNConfig(seed=config.seed),
            estimator=estimator,
            sample_spec=GraphSampleSpec(structure=structure, seed=config.seed),
            true_edges_by_dataset=corpus.hidden.true_edges if leaky else None,
        )
        card = _run_one(name, model, train, validation, test, config.task)
        results.append(
            AblationResult(
                name=name,
                question=question,
                reference="temporal_gnn",
                leaky=leaky,
                card=card.headline(),
                delta=_delta(card, baselines.get("temporal_gnn")),
                note=(
                    "conditions on the generator's true overlay; upper bound only"
                    if leaky
                    else ""
                ),
                elapsed_s=time.perf_counter() - started,
            )
        )
        logger.info("ablation_done", name=name, seconds=round(results[-1].elapsed_s, 1))
    return results


def _observation_ablations(
    config: Any, baselines: dict[str, ScoreCard]
) -> list[AblationResult]:
    """Ablations that change what is observable, and so need the corpus rebuilt.

    These cannot be done with a column mask. Dropping the balance-sheet columns
    while leaving every ratio that divides by the buffer in place would hide the
    number and keep the information; the observation spec instead flattens the
    balance sheet before any feature is computed, so the ratios lose their
    cross-sectional content too.
    """
    variants = (
        (
            "no_balance_sheet",
            ObservationSpec(balance_sheet=False),
            "how much of the result rests on the disclosure assumption?",
        ),
        (
            "no_shock_descriptor",
            ObservationSpec(shock_descriptor=False),
            "can the model find the origin itself?",
        ),
    )

    results: list[AblationResult] = []
    for name, observation, question in variants:
        started = time.perf_counter()
        corpus = build_corpus(
            config.seeds,
            scale=config.scale,
            magnitude=config.magnitude,
            observation=observation,
            task=config.task,
        )
        split = make_temporal_split(corpus, config.split)
        assert_split_clean(corpus, split)
        card = _run_one(
            name,
            DiscreteTimeHazard(config.task),
            split.examples(corpus, "train"),
            split.examples(corpus, "validation"),
            split.examples(corpus, "test"),
            config.task,
        )
        results.append(
            AblationResult(
                name=name,
                question=question,
                reference="discrete_hazard",
                card=card.headline(),
                delta=_delta(card, baselines.get("discrete_hazard")),
                note="corpus rebuilt under a restricted observation spec",
                elapsed_s=time.perf_counter() - started,
            )
        )
        logger.info("ablation_done", name=name, seconds=round(results[-1].elapsed_s, 1))
    return results
