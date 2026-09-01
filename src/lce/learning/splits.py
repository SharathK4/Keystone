"""Temporal train / validation / test partitioning.

The failure mode this module exists to prevent is the one that quietly ruins
most contagion results: a model that scores well because something it saw during
training had already told it the answer. There are three separate ways that can
happen here, and each needs its own barrier.

1. **Future events inside an example.** Handled upstream, in
   :mod:`lce.learning.problem`: features are a function of the filtration at the
   origin, and :func:`~lce.learning.problem.audit_leakage` checks it.

2. **Future examples during training.** Handled here. Examples are ordered on a
   single clock - ``absolute_origin = epoch(dataset) + t_0(scenario)`` - and the
   blocks are cut in that order, train before validation before test.

3. **Shared entities.** Also handled here. Blocks are cut on **dataset**
   boundaries, so no merchant, obligation or payment history is ever shared
   between two blocks - the merchants a model trains on do not exist at test
   time. Note that the generator numbers merchants *positionally*, so the string
   ``m0007`` recurs in every network; it names the eighth node of whichever
   dataset it belongs to, not a persistent entity. Identity is therefore checked
   dataset-qualified, and it never reaches a model in any case: an example
   carries a numeric feature matrix, and the id list beside it is a reporting
   label.

The purge band
--------------
Between consecutive blocks a band of ``purge_hours`` is dropped, so that no
training example's label window ``(t_0, t_0 + T]`` can reach into a test
example's feature window. With the default epoch stride the datasets are already
far enough apart that the band drops nothing - which is the point:
:func:`verify_split` proves the guarantee holds rather than assuming it, and the
band is what keeps that true if the stride is ever tightened.

Why datasets are stamped onto one clock
---------------------------------------
Within a dataset the ordering is real simulation time. Across datasets it is a
protocol convention: separate synthetic ecosystems do not literally follow one
another. The convention is stated plainly rather than dressed up, because what
it buys is precise and checkable - every training label is resolved before any
test example begins.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from lce.errors import ValidationError
from lce.learning.dataset import ContagionExample, ExampleCorpus
from lce.logging import get_logger

logger = get_logger(__name__)


class SplitName(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


@dataclass(frozen=True, slots=True)
class SplitSpec:
    """How the corpus is cut.

    Fractions are over *examples*, not datasets, but the cut lands on a dataset
    boundary: entity disjointness is the stronger guarantee and it wins when the
    two conflict.
    """

    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    purge_hours: float | None = None
    """Gap enforced between blocks. ``None`` derives it from the longest label
    window in the corpus, which is the smallest value that is always sufficient."""

    def __post_init__(self) -> None:
        if not 0.0 < self.train_fraction < 1.0:
            raise ValidationError("train_fraction must lie strictly in (0, 1)")
        if not 0.0 <= self.validation_fraction < 1.0:
            raise ValidationError("validation_fraction must lie in [0, 1)")
        if self.train_fraction + self.validation_fraction >= 1.0:
            raise ValidationError(
                "train and validation fractions leave nothing for the test block"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_fraction": self.train_fraction,
            "validation_fraction": self.validation_fraction,
            "purge_hours": self.purge_hours,
        }


@dataclass(slots=True)
class TemporalSplit:
    """Indices into ``corpus.examples``, partitioned in origin-time order."""

    train: list[int] = field(default_factory=list)
    validation: list[int] = field(default_factory=list)
    test: list[int] = field(default_factory=list)
    purged: list[int] = field(default_factory=list)
    purge_hours: float = 0.0
    boundaries: tuple[float, float] = (0.0, 0.0)
    dataset_blocks: dict[str, str] = field(default_factory=dict)

    def indices(self, name: SplitName | str) -> list[int]:
        return {
            SplitName.TRAIN: self.train,
            SplitName.VALIDATION: self.validation,
            SplitName.TEST: self.test,
        }[SplitName(name)]

    def examples(
        self, corpus: ExampleCorpus, name: SplitName | str
    ) -> list[ContagionExample]:
        return [corpus.examples[i] for i in self.indices(name)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_train": len(self.train),
            "n_validation": len(self.validation),
            "n_test": len(self.test),
            "n_purged": len(self.purged),
            "purge_hours": self.purge_hours,
            "boundaries": list(self.boundaries),
            "dataset_blocks": dict(self.dataset_blocks),
        }


def _dataset_order(corpus: ExampleCorpus) -> list[str]:
    """Dataset ids in epoch order - the clock the split is cut on."""
    earliest: dict[str, float] = {}
    for example in corpus.examples:
        current = earliest.get(example.dataset_id)
        if current is None or example.absolute_origin < current:
            earliest[example.dataset_id] = example.absolute_origin
    return sorted(earliest, key=lambda d: (earliest[d], d))


def make_temporal_split(
    corpus: ExampleCorpus, spec: SplitSpec = SplitSpec()
) -> TemporalSplit:
    """Cut the corpus into train / validation / test in origin-time order.

    Datasets are assigned whole to blocks, in epoch order, until each block's
    example quota is met. Every block is guaranteed non-empty: with fewer
    datasets than blocks the split is rejected rather than silently producing a
    test set of size zero, which would make every downstream number meaningless.
    """
    if not corpus.examples:
        raise ValidationError("cannot split an empty corpus")

    order = _dataset_order(corpus)
    if len(order) < 3:
        raise ValidationError(
            f"a temporal split needs at least 3 datasets so each block gets one; "
            f"the corpus has {len(order)}"
        )

    by_dataset: dict[str, list[int]] = {d: [] for d in order}
    for index, example in enumerate(corpus.examples):
        by_dataset[example.dataset_id].append(index)

    total = len(corpus.examples)
    train_quota = spec.train_fraction * total
    validation_quota = (spec.train_fraction + spec.validation_fraction) * total

    split = TemporalSplit()
    # Reserve one dataset per block up front. Without this a quota of 0.6/0.2 on
    # four datasets can hand three to train and leave validation empty.
    max_train_datasets = len(order) - 2
    max_validation_datasets = len(order) - 1

    seen = 0
    n_train_datasets = 0
    for n_assigned_datasets, dataset_id in enumerate(order):
        indices = by_dataset[dataset_id]
        if seen < train_quota and n_train_datasets < max_train_datasets:
            block = SplitName.TRAIN
            n_train_datasets += 1
        elif seen < validation_quota and n_assigned_datasets < max_validation_datasets:
            block = SplitName.VALIDATION
        else:
            block = SplitName.TEST
        split.indices(block).extend(indices)
        split.dataset_blocks[dataset_id] = str(block)
        seen += len(indices)

    if not split.validation:
        raise ValidationError("temporal split produced an empty validation block")
    if not split.test:
        raise ValidationError("temporal split produced an empty test block")

    purge = (
        spec.purge_hours
        if spec.purge_hours is not None
        else max(e.remaining_hours for e in corpus.examples)
    )
    split.purge_hours = float(purge)

    validation_start = min(corpus.examples[i].absolute_origin for i in split.validation)
    test_start = min(corpus.examples[i].absolute_origin for i in split.test)
    split.boundaries = (validation_start, test_start)

    split.train, purged_train = _purge(corpus, split.train, validation_start, purge)
    split.validation, purged_validation = _purge(
        corpus, split.validation, test_start, purge
    )
    split.purged = sorted(purged_train + purged_validation)

    logger.info("temporal_split", **split.to_dict())
    return split


def _purge(
    corpus: ExampleCorpus, indices: Sequence[int], next_start: float, purge_hours: float
) -> tuple[list[int], list[int]]:
    """Drop examples whose label window reaches within ``purge_hours`` of ``next_start``."""
    kept: list[int] = []
    dropped: list[int] = []
    for index in indices:
        example = corpus.examples[index]
        label_end = example.absolute_origin + example.remaining_hours
        if label_end + purge_hours > next_start:
            dropped.append(index)
        else:
            kept.append(index)
    return kept, dropped


# ------------------------------------------------------------------ assertions


@dataclass(slots=True)
class SplitAudit:
    """Verification of the guarantees the split is supposed to provide."""

    checks: dict[str, bool] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return all(self.checks.values())

    def failures(self) -> list[str]:
        return sorted(name for name, ok in self.checks.items() if not ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "checks": dict(self.checks),
            "failures": self.failures(),
            "detail": dict(self.detail),
        }


def verify_split(corpus: ExampleCorpus, split: TemporalSplit) -> SplitAudit:
    """Check every property the split claims, rather than trusting construction.

    ``labels_resolve_before_test`` is the one that matters: every training and
    validation label window must close strictly before the earliest test origin.
    If that holds, no test-time information could have been available at fit
    time, whatever the model does with it.
    """
    audit = SplitAudit()

    def times(indices: Sequence[int]) -> tuple[float, float]:
        if not indices:
            return (float("inf"), float("-inf"))
        origins = [corpus.examples[i].absolute_origin for i in indices]
        ends = [
            corpus.examples[i].absolute_origin + corpus.examples[i].remaining_hours
            for i in indices
        ]
        return (min(origins), max(ends))

    train_start, train_end = times(split.train)
    validation_start, validation_end = times(split.validation)
    test_start, _ = times(split.test)

    fitted = list(split.train) + list(split.validation)
    _, fitted_end = times(fitted)

    audit.checks["blocks_non_empty"] = bool(
        split.train and split.validation and split.test
    )
    audit.checks["train_precedes_validation"] = train_end < validation_start
    audit.checks["validation_precedes_test"] = validation_end < test_start
    audit.checks["labels_resolve_before_test"] = fitted_end < test_start

    def datasets(indices: Sequence[int]) -> set[str]:
        return {corpus.examples[i].dataset_id for i in indices}

    train_datasets = datasets(split.train)
    validation_datasets = datasets(split.validation)
    test_datasets = datasets(split.test)
    audit.checks["datasets_disjoint"] = (
        not (train_datasets & test_datasets)
        and not (train_datasets & validation_datasets)
        and not (validation_datasets & test_datasets)
    )

    def entities(indices: Sequence[int]) -> set[tuple[str, str]]:
        """Dataset-qualified merchant identities.

        The generator numbers merchants positionally - ``m0007`` is the eighth
        node of *whatever* network it belongs to - so the bare ids repeat across
        datasets and comparing them would report a collision that does not
        exist. Qualifying by dataset gives the entity that is actually meant.
        Bare ids never reach a model in any case: an example carries a numeric
        feature matrix, and ``merchant_ids`` is a reporting label indexed
        alongside it.
        """
        out: set[tuple[str, str]] = set()
        for i in indices:
            example = corpus.examples[i]
            out.update((example.dataset_id, m) for m in example.merchant_ids)
        return out

    audit.checks["entities_disjoint"] = not (
        entities(split.train) & entities(split.test)
    )

    assigned = set(split.train) | set(split.validation) | set(split.test) | set(split.purged)
    audit.checks["every_example_accounted_for"] = assigned == set(
        range(len(corpus.examples))
    )

    audit.detail = {
        "train_window": [train_start, train_end],
        "validation_window": [validation_start, validation_end],
        "test_start": test_start,
        "gap_to_test_hours": test_start - fitted_end if fitted else None,
        "n_train_datasets": len(train_datasets),
        "n_validation_datasets": len(validation_datasets),
        "n_test_datasets": len(test_datasets),
    }
    return audit


def assert_split_clean(corpus: ExampleCorpus, split: TemporalSplit) -> SplitAudit:
    """Verify, and raise if any guarantee is violated."""
    audit = verify_split(corpus, split)
    if not audit.clean:
        raise ValidationError(
            f"temporal split violates: {', '.join(audit.failures())}",
            **audit.detail,
        )
    return audit
