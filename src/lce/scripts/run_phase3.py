"""Phase-3 experiment runner.

Builds the corpus, audits the leakage barrier, cuts and verifies the temporal
split, fits every model, calibrates on validation, scores once on test, recovers
the latent dependency structure, and optionally runs the ablation suite.

    python scripts/run_phase3.py --seeds 101 102 ... --ablations --out reports/phase3.json

The default seed list is the reporting configuration; a smaller one is fine for a
smoke run but the test block gets thin fast, and with a downstream positive rate
under one percent a three-dataset test block is not a measurement.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lce.learning.experiment import MODEL_KEYS, Phase3Config, run_phase3
from lce.learning.problem import ObservationSpec, PredictionTask
from lce.learning.splits import SplitSpec
from lce.logging import configure_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Phase-3 experiment")
    parser.add_argument("--seeds", nargs="*", type=int, default=list(range(101, 125)))
    parser.add_argument("--scale", default="small")
    parser.add_argument("--magnitude", type=float, default=2.0)
    parser.add_argument("--intervals", type=int, default=8)
    parser.add_argument("--models", nargs="*", default=list(MODEL_KEYS))
    parser.add_argument("--ablations", action="store_true")
    parser.add_argument("--full-ablations", action="store_true", dest="full_ablations")
    parser.add_argument("--out", default="reports/phase3.json")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    # force: importing the package may already have configured logging at its
    # default level, and configure_logging is otherwise idempotent.
    configure_logging(level=args.log_level, force=True)

    config = Phase3Config(
        seeds=tuple(args.seeds),
        scale=args.scale,
        magnitude=args.magnitude,
        task=PredictionTask(n_hazard_intervals=args.intervals),
        observation=ObservationSpec(),
        split=SplitSpec(),
        models=tuple(args.models),
    )
    report = run_phase3(
        config,
        run_ablations=args.ablations or args.full_ablations,
        full_ablations=args.full_ablations,
    )

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    corpus = report.corpus
    print(f"\nconfig_hash        {corpus['config_hash']}")
    print(
        f"corpus             {corpus['n_examples']} examples over "
        f"{corpus['n_datasets']} datasets "
        f"({corpus['n_train']}/{corpus['n_validation']}/{corpus['n_test']} split)"
    )
    print(
        f"positives          {corpus['n_positive']} of {corpus['n_scored_nodes']} "
        f"({corpus['positive_rate']:.2%}); downstream "
        f"{corpus['n_positive_downstream']} ({corpus['downstream_positive_rate']:.2%})"
    )
    audit = report.split_audit
    print(
        f"split audit        clean={audit['clean']} "
        f"gap_to_test={audit['detail']['gap_to_test_hours']:.0f}h"
    )
    print(
        f"leakage audit      clean={audit['window_audit']['clean']} "
        f"({audit['window_audit']['window_probes']} window probes, "
        f"{audit['window_audit']['perturbation_probes']} perturbation probes)"
    )

    # Ranked by the downstream number: the pooled column is dominated by the
    # directly-shocked merchants, who are trivially identifiable.
    print()
    print(
        f"{'model':20} {'PR-AUC':>7} {'95% CI':>16} {'down':>7} {'95% CI':>16} "
        f"{'Brier':>8} {'ECE':>7} {'MAE h':>7} {'C-idx':>6}"
    )
    for row in report.leaderboard(key="downstream_pr_auc"):
        print(
            f"{row['model']:20} {fmt(row['pr_auc'])} {ci(row['pr_auc_ci'])} "
            f"{fmt(row['downstream_pr_auc'])} {ci(row['downstream_pr_auc_ci'])} "
            f"{fmt(row['brier'], 8, 5)} {fmt(row['ece'], 7, 4)} "
            f"{fmt(row['timing_mae_hours'], 7, 1)} {fmt(row['concordance'], 6)}"
        )

    dependency = report.dependency["pooled"]
    print(
        f"\ndependency (unsupervised, pre-origin events only): "
        f"theta MAE {dependency['pass_through_mae']:.3f}, "
        f"rho {dependency['pass_through_spearman']:.3f}, "
        f"edge recall {dependency['edge_recall']:.3f}, "
        f"lag MAE {dependency['lag_mae_hours']:.1f}h"
    )
    supervised = report.dependency.get("supervised_upper_bound") or {}
    if "pass_through_mae" in supervised:
        print(
            f"supervised upper bound (trains on hidden labels): "
            f"theta MAE {supervised['pass_through_mae']:.3f}, "
            f"rho {supervised['pass_through_spearman']:.3f}"
        )

    for result in report.ablations.get("results", []):
        delta = result.get("delta", {})
        marker = " [leaky upper bound]" if result.get("leaky") else ""
        print(
            f"\nablation {result['name']:20} vs {result['reference']}{marker}"
            f"\n  {result['question']}"
            f"\n  pr_auc {_signed(delta.get('pr_auc'))}  "
            f"downstream {_signed(delta.get('downstream_pr_auc'))}  "
            f"timing {_signed(delta.get('timing_mae_hours'), places=1)}h"
        )

    print(f"\nwritten to {path}  ({report.elapsed_ms / 1000.0:.0f}s)")
    return 0


def fmt(value: object, width: int = 7, places: int = 3) -> str:
    """Right-aligned number, or a dash where the metric was undefined."""
    if isinstance(value, float):
        return f"{value:{width}.{places}f}"
    return "-".rjust(width)


def ci(interval: object, width: int = 16) -> str:
    """``[lo, hi]`` bootstrap interval, or a dash where none could be computed."""
    if not isinstance(interval, dict):
        return "-".rjust(width)
    return f"[{interval['lo']:.3f}, {interval['hi']:.3f}]".rjust(width)


def _signed(value: object, places: int = 3) -> str:
    if not isinstance(value, float):
        return "n/a"
    return f"{value:+.{places}f}"


if __name__ == "__main__":
    raise SystemExit(main())
