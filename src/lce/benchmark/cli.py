"""Benchmark CLI commands.

Kept in the benchmark package rather than inlined into ``lce.cli`` so the
heavier imports (pandas, pyarrow, the simulator) are only paid for by commands
that actually need them.

    lce bench generate --scale small --seed 2025 --out benchmarks
    lce bench scenario --dataset <dir> --family concentrated_shock
    lce bench stats    --dataset <dir>
    lce bench validate --dataset <dir>
    lce bench export   --dataset <dir> --format csv
    lce bench load     --dataset <dir>
    lce bench replay   --dataset <dir> --scenario <scenario_id>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("benchmarks")


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _sim_config(horizon_hours: float, seed: int):
    from lce.simulation.engine import SimulationConfig

    return SimulationConfig(horizon_hours=horizon_hours, seed=seed)


def cmd_bench_generate(args: argparse.Namespace) -> int:
    """Generate, validate and export a benchmark dataset with its scenarios."""
    from lce.benchmark.export import export_dataset, export_scenario
    from lce.benchmark.ground_truth import compute_ground_truth
    from lce.benchmark.scales import BenchmarkScale, profile_for, scale_config
    from lce.benchmark.scenarios import baseline_affected_set, scenario_suite
    from lce.benchmark.validation import validate_dataset
    from lce.data.generator import generate_network

    scale = BenchmarkScale(args.scale)
    profile = profile_for(scale)
    config = scale_config(scale, seed=args.seed)
    network = generate_network(config)
    sim = _sim_config(config.horizon_hours, args.seed)

    result = export_dataset(
        network,
        Path(args.out),
        fmt=args.format,
        scale=str(scale),
        streaming=profile.streaming or args.streaming,
    )

    scenarios: list[dict[str, Any]] = []
    if not args.no_scenarios:
        # One baseline, shared by target selection and by every scenario's
        # ground truth. Letting scenario_suite compute its own would filter
        # targets against a differently-seeded run than the one the ground truth
        # is differenced against, so a "healthy" target could turn out to be
        # already failing in the run that actually counts.
        baseline_affected = baseline_affected_set(network.graph, sim)
        for scenario in scenario_suite(
            network.graph,
            dataset_id=network.dataset_version,
            seed=args.seed,
            magnitude=args.magnitude,
            config=sim,
            baseline_affected=baseline_affected,
        ):
            truth = compute_ground_truth(
                scenario,
                true_edges=network.ground_truth_edges,
                config=sim,
                compute_optimum=profile.exhaustive_optimum and not args.no_optimum,
                max_actions=args.max_actions,
            )
            export_scenario(result.directory, scenario, truth)
            scenarios.append(truth.summary())

    payload: dict[str, Any] = {
        "dataset_id": network.dataset_version,
        "scale": str(scale),
        "seed": args.seed,
        "directory": str(result.directory),
        "rows": result.rows,
        "scenarios": scenarios,
    }

    if not args.no_validate:
        report = validate_dataset(network, config=sim, deep=not args.quick)
        payload["validation"] = {
            "passed": report.passed,
            "summary": report.summary(),
            "failures": [c.to_dict() for c in report.failures],
            "warnings": [c.name for c in report.warnings],
        }
        _print(payload)
        return 0 if report.passed else 1

    _print(payload)
    return 0


def cmd_bench_scenario(args: argparse.Namespace) -> int:
    """Build one scenario against an existing dataset and record its truth."""
    from lce.benchmark.export import export_scenario
    from lce.benchmark.ground_truth import compute_ground_truth
    from lce.benchmark.manifest import DatasetManifest
    from lce.benchmark.scenarios import (
        ScenarioFamily,
        ScenarioSpec,
        TargetStrategy,
        build_scenario,
    )
    from lce.data.generator import generate_network

    directory = Path(args.dataset)
    manifest = DatasetManifest.load(directory)
    manifest.verify()

    # Regenerate rather than load: the ground truth needs the generator's latent
    # parameters, which the exported observable tables deliberately omit.
    network = generate_network(manifest.rebuild_config())
    spec = ScenarioSpec(
        family=ScenarioFamily(args.family),
        magnitude=args.magnitude,
        shock_time=args.shock_time,
        delay_hours=args.delay_hours,
        partial_fraction=args.partial_fraction,
        n_targets=args.n_targets,
        target_strategy=TargetStrategy(args.target_strategy),
        explicit_targets=tuple(args.targets or ()),
        seed=args.seed,
    )
    scenario = build_scenario(network.graph, spec, dataset_id=network.dataset_version)
    truth = compute_ground_truth(
        scenario,
        true_edges=network.ground_truth_edges,
        config=_sim_config(network.config.horizon_hours, manifest.seed),
        compute_optimum=not args.no_optimum,
        max_actions=args.max_actions,
    )
    path = export_scenario(directory, scenario, truth)

    _print(
        {
            "scenario_id": scenario.scenario_id,
            "dataset_id": network.dataset_version,
            "written_to": str(path),
            "scenario": scenario.to_dict(),
            "ground_truth": truth.summary(),
        }
    )
    return 0


def cmd_bench_stats(args: argparse.Namespace) -> int:
    """Print network statistics and distributional diagnostics."""
    from lce.benchmark.export import list_scenarios
    from lce.benchmark.manifest import DatasetManifest
    from lce.benchmark.validation import compute_diagnostics
    from lce.data.generator import generate_network

    directory = Path(args.dataset)
    manifest = DatasetManifest.load(directory)
    network = generate_network(manifest.rebuild_config())

    _print(
        {
            "dataset_id": manifest.dataset_id,
            "generator_version": manifest.generator_version,
            "code_version": manifest.code_version,
            "seed": manifest.seed,
            "scale": manifest.scale,
            "created_at": manifest.created_at,
            "scenarios": list_scenarios(directory),
            "diagnostics": compute_diagnostics(network),
        }
    )
    return 0


def cmd_bench_validate(args: argparse.Namespace) -> int:
    """Run the scientific sanity checks. Exit code 1 on any error-level failure."""
    from lce.benchmark.manifest import DatasetManifest
    from lce.benchmark.validation import validate_dataset
    from lce.data.generator import generate_network

    manifest = DatasetManifest.load(Path(args.dataset))
    manifest.verify()
    network = generate_network(manifest.rebuild_config())
    report = validate_dataset(
        network,
        config=_sim_config(network.config.horizon_hours, manifest.seed),
        deep=not args.quick,
    )
    _print(report.to_dict())
    return 0 if report.passed else 1


def cmd_bench_export(args: argparse.Namespace) -> int:
    """Re-export an existing dataset in another format."""
    from lce.benchmark.export import export_dataset
    from lce.benchmark.manifest import DatasetManifest
    from lce.data.generator import generate_network

    manifest = DatasetManifest.load(Path(args.dataset))
    manifest.verify()
    network = generate_network(manifest.rebuild_config())
    result = export_dataset(
        network,
        Path(args.out),
        fmt=args.format,
        scale=manifest.scale,
        streaming=args.streaming,
        include_ground_truth=not args.no_ground_truth,
    )
    _print(result.to_dict())
    return 0


def cmd_bench_load(args: argparse.Namespace) -> int:
    """Load a persisted dataset and report what came back."""
    from lce.benchmark.export import list_scenarios, load_dataset

    graph, manifest = load_dataset(
        Path(args.dataset), with_ground_truth=args.with_ground_truth
    )
    _print(
        {
            "dataset_id": manifest.dataset_id,
            "generator_version": manifest.generator_version,
            "seed": manifest.seed,
            "with_ground_truth": args.with_ground_truth,
            "graph": graph.stats().to_dict(),
            "scenarios": list_scenarios(Path(args.dataset)),
        }
    )
    return 0


def cmd_bench_replay(args: argparse.Namespace) -> int:
    """Deterministically replay a scenario and verify it reproduces."""
    from lce.benchmark.export import replay_scenario

    _print(replay_scenario(Path(args.dataset), args.scenario))
    return 0


def register(subparsers: Any) -> None:
    """Attach the ``bench`` command group to the main parser."""
    from lce.benchmark.scales import BenchmarkScale
    from lce.benchmark.scenarios import ScenarioFamily, TargetStrategy

    bench = subparsers.add_parser("bench", help="Benchmark datasets and scenarios")
    sub = bench.add_subparsers(dest="bench_command", required=True)

    gen = sub.add_parser("generate", help="Generate, validate and export a benchmark")
    gen.add_argument(
        "--scale",
        default=BenchmarkScale.SMALL.value,
        choices=[s.value for s in BenchmarkScale],
    )
    gen.add_argument("--seed", type=int, default=20250101)
    gen.add_argument("--out", default=str(DEFAULT_ROOT))
    gen.add_argument("--format", default="parquet", choices=["parquet", "csv"])
    gen.add_argument("--magnitude", type=float, default=2.0)
    gen.add_argument("--max-actions", type=int, default=2, dest="max_actions")
    gen.add_argument("--streaming", action="store_true")
    gen.add_argument("--no-scenarios", action="store_true", dest="no_scenarios")
    gen.add_argument("--no-validate", action="store_true", dest="no_validate")
    gen.add_argument("--no-optimum", action="store_true", dest="no_optimum")
    gen.add_argument(
        "--quick", action="store_true", help="Skip simulation-based validation checks"
    )
    gen.set_defaults(func=cmd_bench_generate)

    scn = sub.add_parser("scenario", help="Build one scenario on an existing dataset")
    scn.add_argument("--dataset", required=True, help="Dataset directory")
    scn.add_argument(
        "--family",
        default=ScenarioFamily.SINGLE_MISSED_INFLOW.value,
        choices=[f.value for f in ScenarioFamily],
    )
    scn.add_argument("--magnitude", type=float, default=2.0)
    scn.add_argument(
        "--shock-time",
        type=float,
        default=None,
        dest="shock_time",
        help="Omit to place the shock where it bites (see resolve_shock_time).",
    )
    scn.add_argument("--delay-hours", type=float, default=120.0, dest="delay_hours")
    scn.add_argument(
        "--partial-fraction", type=float, default=0.5, dest="partial_fraction"
    )
    scn.add_argument("--n-targets", type=int, default=3, dest="n_targets")
    scn.add_argument(
        "--target-strategy",
        default=TargetStrategy.MOST_CONNECTED.value,
        choices=[s.value for s in TargetStrategy],
        dest="target_strategy",
    )
    scn.add_argument("--targets", nargs="*", default=None)
    scn.add_argument("--max-actions", type=int, default=2, dest="max_actions")
    scn.add_argument("--no-optimum", action="store_true", dest="no_optimum")
    scn.add_argument("--seed", type=int, default=0)
    scn.set_defaults(func=cmd_bench_scenario)

    stats = sub.add_parser("stats", help="Network statistics and diagnostics")
    stats.add_argument("--dataset", required=True)
    stats.set_defaults(func=cmd_bench_stats)

    val = sub.add_parser("validate", help="Run scientific sanity checks")
    val.add_argument("--dataset", required=True)
    val.add_argument("--quick", action="store_true")
    val.set_defaults(func=cmd_bench_validate)

    exp = sub.add_parser("export", help="Re-export a dataset (parquet/csv)")
    exp.add_argument("--dataset", required=True)
    exp.add_argument("--out", default=str(DEFAULT_ROOT))
    exp.add_argument("--format", default="csv", choices=["parquet", "csv"])
    exp.add_argument("--streaming", action="store_true")
    exp.add_argument(
        "--no-ground-truth", action="store_true", dest="no_ground_truth"
    )
    exp.set_defaults(func=cmd_bench_export)

    load = sub.add_parser("load", help="Load a persisted dataset")
    load.add_argument("--dataset", required=True)
    load.add_argument(
        "--with-ground-truth", action="store_true", dest="with_ground_truth"
    )
    load.set_defaults(func=cmd_bench_load)

    replay = sub.add_parser("replay", help="Deterministically replay a scenario")
    replay.add_argument("--dataset", required=True)
    replay.add_argument("--scenario", required=True)
    replay.set_defaults(func=cmd_bench_replay)
