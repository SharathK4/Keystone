"""Inference CLI: train once, export, then serve from the artifact.

    lce infer export --seeds 101 ... --out artifacts/contagion
    lce infer list --artifacts artifacts
    lce infer info --artifacts artifacts
    lce infer check --artifacts artifacts

``export`` is the only command that touches training code. The rest read a
bundle, which is the whole point of the separation: once a model is exported,
every downstream operation needs the artifact and the inference package and
nothing else.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def cmd_export(args: argparse.Namespace) -> int:
    """Train a Phase-3 hazard model on a corpus and export it for serving.

    Runs the Phase-3 protocol unchanged - build a corpus, cut the temporal split,
    fit on train, calibrate and threshold on validation - and then writes the
    fitted weights, the calibration map and the threshold as one bundle. Test is
    scored once and the score travels with the artifact, so a served model
    carries the number it was accepted on.
    """
    from lce.inference.export import export_hazard_model
    from lce.learning.baselines import DiscreteTimeHazard
    from lce.learning.dataset import build_corpus
    from lce.learning.evaluation import evaluate_forecasts
    from lce.learning.experiment import fit_and_calibrate
    from lce.learning.splits import SplitSpec, assert_split_clean, make_temporal_split

    overrides = json.loads(args.generator_overrides) if args.generator_overrides else None
    corpus = build_corpus(args.seeds, scale=args.scale, overrides=overrides)
    split = make_temporal_split(corpus, SplitSpec(args.train_fraction, args.validation_fraction))
    assert_split_clean(corpus, split)

    train = split.examples(corpus, "train")
    validation = split.examples(corpus, "validation")
    test = split.examples(corpus, "test")

    model = DiscreteTimeHazard(corpus.task)
    fitted = fit_and_calibrate("discrete_hazard", model, train, validation)
    card = evaluate_forecasts(
        test,
        fitted.model.predict_all(test),
        model="discrete_hazard",
        split="test",
        task=corpus.task,
        calibrator=fitted.calibrator,
        threshold=fitted.threshold,
        n_bootstrap=args.bootstrap,
    )

    path = export_hazard_model(
        model,
        Path(args.out),
        name=args.name,
        calibrator=fitted.calibrator,
        threshold=fitted.threshold,
        dataset_version=corpus.dataset_ids()[0] if corpus.dataset_ids() else None,
        seeds=list(args.seeds),
        metrics=card.headline(),
        training={
            "scale": args.scale,
            "n_examples": len(corpus),
            "n_train": len(train),
            "n_validation": len(validation),
            "n_test": len(test),
            "split": split.to_dict(),
            "corpus": corpus.summary(),
        },
    )
    _print(
        {
            "artifact": str(path),
            "model_version": model.model_version,
            "calibrator": fitted.calibrator.to_dict(),
            "threshold": fitted.threshold,
            "test_metrics": card.headline(),
        }
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Every bundle under the artifact root, newest first."""
    from lce.inference.artifact import list_artifacts

    _print({"root": args.artifacts, "artifacts": list_artifacts(Path(args.artifacts))})
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Load a bundle through the service and report what it is."""
    from lce.inference.service import InferenceService

    service = InferenceService(Path(args.artifacts), version=args.model_version)
    _print(service.health())
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Verify integrity and schema compatibility without serving anything.

    Exits non-zero on a bad bundle, so it can gate a deployment.
    """
    from lce.errors import LCEError
    from lce.inference.artifact import load_artifact, resolve_artifact
    from lce.learning.features import FEATURE_SCHEMA_VERSION

    try:
        artifact = load_artifact(
            resolve_artifact(Path(args.artifacts), args.model_version),
            expected_schema=FEATURE_SCHEMA_VERSION,
        )
    except LCEError as exc:
        _print({"ok": False, "error": exc.to_dict()})
        return 1
    _print({"ok": True, **artifact.summary()})
    return 0


def register(subparsers: Any) -> None:
    """Attach the ``infer`` command group."""
    group = subparsers.add_parser("infer", help="Model artifacts and inference")
    sub = group.add_subparsers(dest="infer_command", required=True)

    export = sub.add_parser("export", help="Train and export a servable model")
    export.add_argument("--seeds", nargs="*", type=int, default=list(range(101, 113)))
    export.add_argument("--scale", default="small", choices=["small", "medium", "large"])
    export.add_argument("--out", default="artifacts/contagion")
    export.add_argument("--name", default="contagion_hazard")
    export.add_argument("--train-fraction", type=float, default=0.6, dest="train_fraction")
    export.add_argument(
        "--validation-fraction", type=float, default=0.2, dest="validation_fraction"
    )
    export.add_argument("--bootstrap", type=int, default=200)
    export.add_argument(
        "--generator-overrides", default=None, dest="generator_overrides",
        help='JSON of generator overrides, e.g. \'{"n_merchants": 40}\'.',
    )
    export.set_defaults(func=cmd_export)

    listing = sub.add_parser("list", help="List exported artifacts")
    listing.add_argument("--artifacts", default="artifacts")
    listing.set_defaults(func=cmd_list)

    info = sub.add_parser("info", help="Load an artifact and report it")
    info.add_argument("--artifacts", default="artifacts")
    info.add_argument("--model-version", default=None, dest="model_version")
    info.set_defaults(func=cmd_info)

    check = sub.add_parser("check", help="Verify integrity and schema compatibility")
    check.add_argument("--artifacts", default="artifacts")
    check.add_argument("--model-version", default=None, dest="model_version")
    check.set_defaults(func=cmd_check)
