"""Phase-3 CLI commands.

    lce learn spec                     print the problem specification
    lce learn build --out DIR          build and persist a corpus of examples
    lce learn split                    build a corpus and audit the temporal split
    lce learn run --ablations          the full experiment, scored once on test
    lce learn dependency               task 3 only: recover the latent structure

Kept out of ``lce.cli`` so the heavier imports (scipy, torch, the simulator) are
paid for only by commands that need them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _config(args: argparse.Namespace):
    from lce.learning.experiment import MODEL_KEYS, Phase3Config
    from lce.learning.problem import ObservationSpec, PredictionTask
    from lce.learning.splits import SplitSpec

    return Phase3Config(
        seeds=tuple(args.seeds),
        scale=args.scale,
        magnitude=args.magnitude,
        observation=ObservationSpec(
            balance_sheet=not args.no_balance_sheet,
            shock_descriptor=not args.no_shock_descriptor,
        ),
        task=PredictionTask(n_hazard_intervals=args.intervals),
        split=SplitSpec(
            train_fraction=args.train_fraction,
            validation_fraction=args.validation_fraction,
        ),
        models=tuple(args.models or MODEL_KEYS),
        seed=args.seed,
    )


def cmd_learn_spec(args: argparse.Namespace) -> int:
    """Print exactly what is observable, what is latent, and what is predicted."""
    from lce.learning.features import (
        FEATURE_GROUPS,
        NETWORK_DEPENDENT_COLUMNS,
        feature_summary,
    )
    from lce.learning.problem import (
        DEFAULT_TASK,
        LATENT_PROFILE_FIELDS,
        OBSERVABLE_PROFILE_FIELDS,
        ObservationSpec,
    )

    _print(
        {
            "origin": "shock onset t0; features are a function of the filtration at t0-",
            "observable": {
                "payments": "every event with t < t0, history plus the no-shock baseline stream",
                "obligations": "the book as issued (issued_t < t0), read off the unperturbed graph",
                "merchant_fields": sorted(OBSERVABLE_PROFILE_FIELDS),
                "shock": "origin merchants, onset, magnitude, kind - the operator's trigger",
            },
            "latent": {
                "dependency_overlay": "pass_through, conditional_probability, lag law, reliability",
                "merchant_fields": sorted(LATENT_PROFILE_FIELDS),
                "everything_at_or_after_the_origin": True,
                "perturbed_obligation_book": True,
                "ground_truth": "affected set, hit times, cascade depth, disrupted volume",
            },
            "targets": {
                "task_1": "F_i(t) = P(constrained within t hours of the origin)",
                "task_2": "tau_i, time to constraint, right-censored at the horizon",
                "task_3": "theta_ij, q_ij and the lag law, estimated unsupervised",
            },
            "task": DEFAULT_TASK.to_dict(),
            "observation_default": ObservationSpec().to_dict(),
            "feature_groups": list(FEATURE_GROUPS),
            "network_dependent_columns": sorted(NETWORK_DEPENDENT_COLUMNS),
            "features": feature_summary(),
        }
    )
    return 0


def cmd_learn_build(args: argparse.Namespace) -> int:
    """Build a corpus of examples and write the observable half to disk."""
    from lce.learning.dataset import build_corpus, save_corpus

    config = _config(args)
    corpus = build_corpus(
        config.seeds,
        scale=config.scale,
        magnitude=config.magnitude,
        observation=config.observation,
        task=config.task,
    )
    directory = save_corpus(corpus, Path(args.out))
    _print(
        {
            "directory": str(directory),
            "config_hash": config.config_hash,
            "summary": corpus.summary(),
            "datasets": {
                d: {k: v for k, v in meta.items() if k != "leakage_audit"}
                for d, meta in corpus.datasets.items()
            },
        }
    )
    return 0


def cmd_learn_split(args: argparse.Namespace) -> int:
    """Build a corpus, cut the temporal split, and verify every guarantee."""
    from lce.learning.dataset import build_corpus
    from lce.learning.experiment import audit_corpus
    from lce.learning.splits import make_temporal_split, verify_split

    config = _config(args)
    corpus = build_corpus(
        config.seeds,
        scale=config.scale,
        magnitude=config.magnitude,
        observation=config.observation,
        task=config.task,
    )
    split = make_temporal_split(corpus, config.split)
    audit = verify_split(corpus, split)
    _print(
        {
            "config_hash": config.config_hash,
            "corpus": corpus.summary(),
            "split": split.to_dict(),
            "split_audit": audit.to_dict(),
            "leakage_audit": audit_corpus(corpus, config),
        }
    )
    return 0 if audit.clean else 1


def cmd_learn_run(args: argparse.Namespace) -> int:
    """Run the full protocol: build, audit, split, fit, calibrate, score once."""
    from lce.learning.experiment import Phase3Config, run_phase3

    config: Phase3Config = _config(args)
    report = run_phase3(
        config,
        run_ablations=args.ablations or args.full_ablations,
        full_ablations=args.full_ablations,
    )
    payload = report.to_dict()
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        payload = {"written_to": str(path), "leaderboard": report.leaderboard()}
    _print(payload)
    return 0


def cmd_learn_dependency(args: argparse.Namespace) -> int:
    """Task 3 alone: unsupervised structure recovery, with its upper bound."""
    from lce.learning.dataset import build_corpus
    from lce.learning.experiment import _dependency_section
    from lce.learning.pointprocess import HawkesDependencyEstimator
    from lce.learning.splits import assert_split_clean, make_temporal_split

    config = _config(args)
    corpus = build_corpus(
        config.seeds,
        scale=config.scale,
        magnitude=config.magnitude,
        observation=config.observation,
        task=config.task,
    )
    split = make_temporal_split(corpus, config.split)
    assert_split_clean(corpus, split)
    _print(
        {
            "config_hash": config.config_hash,
            "dependency": _dependency_section(
                corpus,
                split.examples(corpus, "train"),
                split.examples(corpus, "test"),
                HawkesDependencyEstimator(),
            ),
        }
    )
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    from lce.benchmark.scales import BenchmarkScale

    parser.add_argument(
        "--seeds",
        nargs="*",
        type=int,
        default=list(range(101, 125)),
        help="Dataset seeds, in the order they are stamped onto the corpus clock.",
    )
    parser.add_argument(
        "--scale", default=str(BenchmarkScale.SMALL), choices=[s.value for s in BenchmarkScale]
    )
    parser.add_argument("--magnitude", type=float, default=2.0)
    parser.add_argument("--intervals", type=int, default=8, help="Hazard intervals.")
    parser.add_argument("--train-fraction", type=float, default=0.6, dest="train_fraction")
    parser.add_argument(
        "--validation-fraction", type=float, default=0.2, dest="validation_fraction"
    )
    parser.add_argument("--seed", type=int, default=20250101)
    parser.add_argument(
        "--no-balance-sheet", action="store_true", dest="no_balance_sheet"
    )
    parser.add_argument(
        "--no-shock-descriptor", action="store_true", dest="no_shock_descriptor"
    )
    parser.add_argument("--models", nargs="*", default=None)


def register(subparsers: Any) -> None:
    """Attach the ``learn`` command group to the main parser."""
    learn = subparsers.add_parser("learn", help="Phase-3 contagion learning")
    sub = learn.add_subparsers(dest="learn_command", required=True)

    spec = sub.add_parser("spec", help="Print the problem specification")
    spec.set_defaults(func=cmd_learn_spec)

    build = sub.add_parser("build", help="Build and persist a corpus of examples")
    _common(build)
    build.add_argument("--out", default="benchmarks/phase3")
    build.set_defaults(func=cmd_learn_build)

    split = sub.add_parser("split", help="Audit the temporal split")
    _common(split)
    split.set_defaults(func=cmd_learn_split)

    run = sub.add_parser("run", help="Run the full Phase-3 experiment")
    _common(run)
    run.add_argument("--ablations", action="store_true")
    run.add_argument("--full-ablations", action="store_true", dest="full_ablations")
    run.add_argument("--out", default=None, help="Write the full report JSON here.")
    run.set_defaults(func=cmd_learn_run)

    dependency = sub.add_parser("dependency", help="Task 3: latent structure recovery")
    _common(dependency)
    dependency.set_defaults(func=cmd_learn_dependency)
