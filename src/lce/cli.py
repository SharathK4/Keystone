"""Command line interface.

Everything the API can do, plus the research workflows that do not belong in an
HTTP request (running a full experiment, training the GNN, initialising the
database).

    lce db init                    create tables directly (dev only; prefer alembic)
    lce db check                   ping the database
    lce generate --n 60            generate and store a dataset
    lce experiment --n 40          run the full measured pipeline
    lce demo                       the end-to-end narrative, no database needed
    lce serve                      run the API with uvicorn
    lce bench generate             build a reproducible benchmark dataset
    lce bench validate             run the scientific sanity checks
    lce bench replay               deterministically replay a scenario
    lce learn spec                 print the Phase-3 learning problem
    lce learn split                audit the temporal train/val/test split
    lce learn run                  fit, calibrate and score the contagion models
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from typing import Any

from lce import __version__
from lce.config import get_settings
from lce.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


# ----------------------------------------------------------------- commands


def cmd_db_init(args: argparse.Namespace) -> int:
    from lce.data.database import create_all, get_engine

    create_all()
    _print({"status": "ok", "dialect": get_engine().dialect.name})
    return 0


def cmd_db_check(args: argparse.Namespace) -> int:
    from lce.data.database import healthcheck

    result = healthcheck()
    _print(result)
    return 0 if result.get("status") == "ok" else 1


def cmd_generate(args: argparse.Namespace) -> int:
    from lce.data.generator import GeneratorConfig
    from lce.data.unit_of_work import UnitOfWork
    from lce.services.network_service import NetworkService

    config = replace(
        GeneratorConfig(),
        n_merchants=args.n,
        seed=args.seed if args.seed is not None else get_settings().random_seed,
        horizon_hours=args.horizon,
    )
    with UnitOfWork() as uow:
        service = NetworkService(uow)
        network = service.generate_and_store(config, notes=args.notes or "")
        _print(network.summary())
    return 0


def cmd_experiment(args: argparse.Namespace) -> int:
    from lce.experiments.config import quick_config
    from lce.experiments.runner import ExperimentRunner

    config = quick_config(
        n_merchants=args.n,
        seed=args.seed if args.seed is not None else get_settings().random_seed,
        n_shocks=args.shocks,
        name=args.name,
    )
    report = ExperimentRunner(config).run()
    _print(report.to_dict())
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(
            json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """The demo narrative end to end, in-memory - no database required."""
    from lce.data.generator import GeneratorConfig, generate_network
    from lce.evaluation.harness import build_ground_truth, evaluate_prediction
    from lce.models.dependency import compare_to_ground_truth, learn_dependencies
    from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
    from lce.optimization.candidates import CandidateConfig, generate_candidates
    from lce.optimization.search import GreedySearch, SearchConfig
    from lce.simulation.counterfactual import CounterfactualEvaluator
    from lce.simulation.engine import SimulationConfig
    from lce.simulation.scenarios import unit_shock

    seed = args.seed if args.seed is not None else get_settings().random_seed
    horizon = args.horizon

    network = generate_network(
        replace(GeneratorConfig(), n_merchants=args.n, seed=seed, horizon_hours=horizon)
    )
    graph = network.graph
    sim_config = replace(SimulationConfig(), horizon_hours=horizon, seed=seed)

    # 1. Learn the dependency structure from history alone.
    learned = learn_dependencies(graph, t_end=0.0)
    recovery = compare_to_ground_truth(learned, network.ground_truth_edges)
    graph.clear_dependencies()
    graph.set_dependencies(learned)

    # 2. Shock the most connected anchor.
    origin = max(
        network.anchors(), key=lambda m: len(graph.descendants_within(m, 3))
    )
    shock = unit_shock(graph, origin, fraction_of_buffer=args.severity)

    # 3. Simulate the truth.
    truth = build_ground_truth(graph, shock, config=sim_config)

    # 4. Predict, and score the prediction.
    prediction = LinearThresholdPropagator(
        replace(PropagationConfig(), horizon_hours=horizon)
    ).predict(graph, shock)
    evaluation = evaluate_prediction(prediction, truth, graph, name="demo")

    # 5. Find the cheapest intervention that stops the cascade.
    candidates = generate_candidates(
        graph,
        shock,
        prediction,
        replace(CandidateConfig(), top_k_nodes=6, max_candidates=30),
        horizon_hours=horizon,
    )
    search = GreedySearch().run(
        CounterfactualEvaluator(graph=graph, shock=shock, config=sim_config),
        candidates.interventions,
        replace(SearchConfig(), max_actions=args.max_actions),
    )

    _print(
        {
            "dataset_version": network.dataset_version,
            "seed": seed,
            "network": {
                "n_merchants": len(graph),
                "n_events": graph.stats().n_payment_events,
                "n_obligations": graph.stats().n_obligations,
            },
            "dependency_recovery": {
                k: round(v, 4) for k, v in recovery.items()
            },
            "shock": {
                "origin": origin,
                "magnitude": shock.total_magnitude,
                "description": shock.description or shock.name,
            },
            "ground_truth": truth.summary(),
            "timeline_affected": {
                f"{t:.0f}h": len(truth.affected_by(t)) for t in (6, 24, 48, 72)
            },
            "prediction": evaluation.headline(),
            "intervention": {
                **search.summary(),
                "actions": [u.describe() for u in search.plan.interventions],
            },
        }
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "lce.api.app:app",
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=args.reload,
        log_config=None,  # structlog owns logging
    )
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lce",
        description="Liquidity Contagion Engine",
    )
    parser.add_argument("--version", action="version", version=f"lce {__version__}")
    parser.add_argument(
        "--log-level", default=None, help="Override LCE_LOG_LEVEL for this command."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    db = sub.add_parser("db", help="Database utilities")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("init", help="Create tables (dev only; prefer alembic)").set_defaults(
        func=cmd_db_init
    )
    db_sub.add_parser("check", help="Ping the database").set_defaults(func=cmd_db_check)

    gen = sub.add_parser("generate", help="Generate and store a dataset")
    gen.add_argument("-n", type=int, default=60, help="Number of merchants")
    gen.add_argument("--seed", type=int, default=None)
    gen.add_argument("--horizon", type=float, default=168.0)
    gen.add_argument("--notes", default="")
    gen.set_defaults(func=cmd_generate)

    exp = sub.add_parser("experiment", help="Run the full measured pipeline")
    exp.add_argument("-n", type=int, default=40, help="Number of merchants")
    exp.add_argument("--seed", type=int, default=None)
    exp.add_argument("--shocks", type=int, default=3)
    exp.add_argument("--name", default="cli")
    exp.add_argument("--out", default=None, help="Write the report JSON here")
    exp.set_defaults(func=cmd_experiment)

    demo = sub.add_parser("demo", help="End-to-end narrative (no database needed)")
    demo.add_argument("-n", type=int, default=60)
    demo.add_argument("--seed", type=int, default=None)
    demo.add_argument("--horizon", type=float, default=168.0)
    demo.add_argument("--severity", type=float, default=2.5)
    demo.add_argument("--max-actions", type=int, default=2, dest="max_actions")
    demo.set_defaults(func=cmd_demo)

    from lce.benchmark import cli as bench_cli

    bench_cli.register(sub)

    from lce.learning import cli as learn_cli

    learn_cli.register(sub)

    serve = sub.add_parser("serve", help="Run the API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(args.log_level or settings.log_level, settings.log_format, force=True)

    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
