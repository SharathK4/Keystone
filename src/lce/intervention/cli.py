"""Phase-4 CLI.

    lce intervene spec                  print the intervention problem
    lce intervene run --profile ...     the full pipeline, written to reports/phase4
    lce intervene systemic --seed 2025  rank load-bearing merchants
    lce intervene providers             what each execution provider can do

Heavier imports stay inside the command bodies so ``lce --help`` does not pay for
the simulator, the solver and the learner.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def cmd_spec(args: argparse.Namespace) -> int:
    """Print the objective, the constraints and the action taxonomy."""
    from lce.domain.enums import InterventionType
    from lce.intervention.problem import InterventionConstraints, ObjectiveSpec
    from lce.intervention.profiles import BUDGETS

    _print(
        {
            "objective": {
                "penalised": "min_a  D(a) + lambda * Cost(a)",
                "constrained": "min_a  Cost(a)  s.t.  D(a) <= epsilon",
                "D": (
                    "the Phase-1 disruption objective: value-weighted delay + "
                    "default count + liquidity deficit-time, evaluated by "
                    "simulating the action"
                ),
                "Cost": "per-action deployed capital or fee, from domain.intervention",
                "default": ObjectiveSpec().to_dict(),
            },
            "constraints": InterventionConstraints().to_dict(),
            "intervention_types": {
                str(t): {
                    "requires_obligation": t
                    in {
                        InterventionType.RECEIVABLE_ACCELERATION,
                        InterventionType.SUPPLIER_TERM_EXTENSION,
                        InterventionType.REPAYMENT_RESTRUCTURE,
                    },
                    "adds_capital": t
                    in {
                        InterventionType.LIQUIDITY_INJECTION,
                        InterventionType.CREDIT_LINE_INCREASE,
                    },
                }
                for t in InterventionType
            },
            "resource_profiles": {str(k): v.to_dict() for k, v in BUDGETS.items()},
            "invariants": [
                "no money creation: non-capital actions conserve obligated principal",
                "liquidity floor: an action may not worsen a merchant's floor breach",
                "deadlines stay inside the horizon",
                "actions are never scheduled before the decision time",
            ],
        }
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Predict, decide, replay and compare; write the run artifact."""
    from lce.benchmark.scenarios import ScenarioFamily
    from lce.intervention.experiment import Phase4Config, run_phase4, write_artifact
    from lce.intervention.problem import ObjectiveSpec
    from lce.intervention.profiles import ResourceProfile
    from lce.intervention.robust import UncertaintySpec

    config = Phase4Config(
        profile=ResourceProfile(args.profile),
        seeds=tuple(args.seeds),
        magnitude=args.magnitude,
        families=tuple(ScenarioFamily(f) for f in args.families) if args.families else None,
        objective=ObjectiveSpec(
            form=args.form,
            lam=args.lam,
            epsilon=args.epsilon,
            epsilon_fraction=args.epsilon_fraction,
        ),
        uncertainty=UncertaintySpec(kappa=args.kappa, seed=args.seed),
        robust=not args.no_robust,
        predictor=args.predictor,
        artifact_root=args.artifacts,
        artifact_version=args.model_version,
        systemic=not args.no_systemic,
        pruning_benchmark=not args.no_pruning,
        seed=args.seed,
    )
    overrides = json.loads(args.generator_overrides) if args.generator_overrides else None
    report = run_phase4(config, generator_overrides=overrides)
    path = write_artifact(report, root=Path(args.out))

    _print(
        {
            "run_id": report.run_id,
            "artifact": str(path),
            "n_scenarios": len(report.scenarios),
            "elapsed_s": round(report.elapsed_s, 1),
            "summary": report.summary(),
        }
    )
    return 0


def cmd_systemic(args: argparse.Namespace) -> int:
    """Rank merchants by the damage a standardised shock at them causes."""
    from lce.benchmark.scales import BenchmarkScale, scale_config
    from lce.data.generator import generate_network
    from lce.optimization.systemic import compute_systemic_importance
    from lce.simulation.engine import SimulationConfig

    generator = scale_config(BenchmarkScale(args.scale), seed=args.seed)
    network = generate_network(generator)
    sim = SimulationConfig(horizon_hours=generator.horizon_hours, seed=args.seed)
    sample = sorted(network.graph.merchant_ids)[: args.sample]
    ranking = compute_systemic_importance(network.graph, config=sim, merchants=sample)

    _print(
        {
            "dataset_id": network.dataset_version,
            "n_sampled": len(sample),
            "baseline_rank_correlation": ranking.baseline_correlations(),
            "note": (
                "a high correlation with throughput means the ranking is largely "
                "a size ranking; it is reported rather than assumed away"
            ),
            "top_by_importance": ranking.ranked(args.top),
            "top_by_scale_normalised": ranking.ranked_by_scale(args.top),
            "detail": ranking.to_dict()["ranking"][: args.top],
        }
    )
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    """Report what each execution provider can actually do here and now."""
    from lce.execution import RazorpayTestProvider, SimulationProvider

    payload: dict[str, Any] = {
        "simulation": SimulationProvider().capabilities(),
    }
    try:
        payload["razorpay_test"] = RazorpayTestProvider().capabilities()
    except Exception as exc:  # unconfigured or live mode: report, do not crash
        payload["razorpay_test"] = {"error": str(exc)}
    payload["note"] = (
        "no live funds move in this phase; the Razorpay provider maps an action "
        "to a Test-Mode request and returns it for a reviewed submission path"
    )
    _print(payload)
    return 0


def register(subparsers: Any) -> None:
    """Attach the ``intervene`` command group."""
    from lce.benchmark.scenarios import ScenarioFamily
    from lce.intervention.experiment import PREDICTORS
    from lce.intervention.profiles import ResourceProfile

    group = subparsers.add_parser("intervene", help="Phase-4 counterfactual intervention")
    sub = group.add_subparsers(dest="intervene_command", required=True)

    spec = sub.add_parser("spec", help="Print the intervention problem")
    spec.set_defaults(func=cmd_spec)

    run = sub.add_parser("run", help="Run the Phase-4 pipeline")
    run.add_argument(
        "--profile",
        default=str(ResourceProfile.SMALL_FAST),
        choices=[p.value for p in ResourceProfile],
    )
    run.add_argument("--seeds", nargs="*", type=int, default=[2025, 7, 99])
    run.add_argument("--magnitude", type=float, default=2.0)
    run.add_argument(
        "--families", nargs="*", default=None, choices=[f.value for f in ScenarioFamily]
    )
    run.add_argument("--form", default="penalised", choices=["penalised", "constrained"])
    run.add_argument("--lam", type=float, default=1.0, help="lambda: cost weight in J.")
    run.add_argument("--epsilon", type=float, default=None)
    run.add_argument("--epsilon-fraction", type=float, default=0.5, dest="epsilon_fraction")
    run.add_argument("--kappa", type=float, default=0.5, help="Robust dispersion penalty.")
    run.add_argument("--predictor", default="propagation", choices=list(PREDICTORS))
    run.add_argument("--artifacts", default=None, help="Artifact root for --predictor artifact.")
    run.add_argument("--model-version", default=None, dest="model_version")
    run.add_argument("--no-robust", action="store_true", dest="no_robust")
    run.add_argument("--no-systemic", action="store_true", dest="no_systemic")
    run.add_argument("--no-pruning", action="store_true", dest="no_pruning")
    run.add_argument("--out", default="reports/phase4")
    run.add_argument("--seed", type=int, default=20250101)
    run.add_argument(
        "--generator-overrides",
        default=None,
        dest="generator_overrides",
        help='JSON of generator overrides, e.g. \'{"n_merchants": 40}\'.',
    )
    run.set_defaults(func=cmd_run)

    systemic = sub.add_parser("systemic", help="Rank load-bearing merchants")
    systemic.add_argument("--scale", default="small", choices=["small", "medium", "large"])
    systemic.add_argument("--seed", type=int, default=2025)
    systemic.add_argument("--sample", type=int, default=40)
    systemic.add_argument("--top", type=int, default=10)
    systemic.set_defaults(func=cmd_systemic)

    providers = sub.add_parser("providers", help="Execution provider capabilities")
    providers.set_defaults(func=cmd_providers)
