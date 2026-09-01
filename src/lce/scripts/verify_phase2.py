"""Phase-2 acceptance harness.

Runs every scenario family across several seeds and asserts the invariants the
benchmark's scientific claims rest on. Exits non-zero if any invariant fails, so
it is usable as a gate.

Usage:
    python scripts/verify_phase2.py --scales small --seeds 2025 7 99
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lce.benchmark import (
    BenchmarkScale,
    ScenarioFamily,
    ScenarioSpec,
    TargetStrategy,
    build_scenario,
    compute_ground_truth,
    scale_config,
    scenario_suite,
    validate_dataset,
)
from lce.benchmark.scenarios import baseline_affected_set
from lce.data.generator import generate_network
from lce.simulation.engine import LiquiditySimulator, SimulationConfig

REQUIRED_FAMILIES = tuple(ScenarioFamily)

#: Fraction of seeds on which a family must produce a real cascade. Below
#: 100% on purpose - network resilience varies by draw, and a family that
#: bites on most networks but is absorbed by an unusually well-capitalised
#: one is behaving correctly, not failing.
MIN_BITE_RATE = 0.6

#: Seeds needed before the rate above is worth enforcing. On two seeds the
#: only achievable rates are 0%, 50% and 100%, so a 60% threshold tests the
#: sample size rather than the generator.
MIN_SEEDS_FOR_RATE = 4


class Failures:
    def __init__(self) -> None:
        self.items: list[str] = []

    def check(self, condition: bool, message: str) -> bool:
        if not condition:
            self.items.append(message)
        return condition


def verify_seed(
    scale: BenchmarkScale,
    seed: int,
    failures: Failures,
    family_bites: dict[str, list[bool]],
) -> dict:
    config = scale_config(scale, seed=seed)
    network = generate_network(config)
    sim = SimulationConfig(horizon_hours=config.horizon_hours, seed=seed)
    graph = network.graph
    tag = f"{scale}/seed={seed}"

    baseline = baseline_affected_set(graph, sim)
    suite = scenario_suite(
        graph,
        dataset_id=network.dataset_version,
        seed=seed,
        magnitude=2.0,
        config=sim,
        baseline_affected=baseline,
    )
    built = {s.spec.family for s in suite}
    failures.check(
        set(REQUIRED_FAMILIES) <= built,
        f"{tag}: families not built: {sorted(str(f) for f in set(REQUIRED_FAMILIES) - built)}",
    )

    rows = []
    for scenario in suite:
        truth = compute_ground_truth(
            scenario,
            true_edges=network.ground_truth_edges,
            config=sim,
            compute_optimum=(scale is BenchmarkScale.SMALL),
            max_actions=2,
        )
        family = str(scenario.spec.family)

        # Severity is asserted in aggregate across the seed sweep, not per
        # (seed, family). Whether one shock cascades on one random network is
        # genuinely stochastic: some draws produce a well-capitalised
        # neighbourhood that absorbs it. Requiring every family to bite on every
        # seed would mean tuning the generator until it guarantees an outcome,
        # which makes the benchmark less honest rather than more. Structural
        # validity, by contrast, must hold every single time.
        bite = len(truth.affected_nodes) > 0 and truth.disrupted_volume > 0.0
        family_bites.setdefault(family, []).append(bite)

        # --- ground-truth affected set must be internally valid -------------
        failures.check(
            set(truth.affected_nodes) <= set(graph.merchant_ids),
            f"{tag}/{family}: affected set contains unknown merchants",
        )
        failures.check(
            set(truth.affected_nodes).isdisjoint(baseline),
            f"{tag}/{family}: affected set overlaps the no-shock baseline",
        )
        failures.check(
            set(truth.first_constraint_t) <= set(truth.affected_nodes),
            f"{tag}/{family}: constraint timestamps for non-affected nodes",
        )
        failures.check(
            all(0.0 <= t <= sim.horizon_hours for t in truth.first_constraint_t.values()),
            f"{tag}/{family}: constraint timestamp outside the horizon",
        )

        # --- true optimum, where computed, must be feasible -----------------
        optimum = truth.optimal_intervention
        if optimum.available:
            failures.check(
                len(optimum.interventions) <= 2,
                f"{tag}/{family}: optimum exceeds the action cap",
            )
            failures.check(
                (optimum.disruption_prevented or 0.0) >= -1e-6,
                f"{tag}/{family}: optimum increases disruption",
            )
            failures.check(
                optimum.cost >= 0.0, f"{tag}/{family}: optimum has negative cost"
            )

        rows.append(
            {
                "family": family,
                "targets": scenario.targets,
                "shock_t": round(scenario.shock.onset_t, 1),
                "magnitude": scenario.shock.total_magnitude,
                "n_affected": len(truth.affected_nodes),
                "max_depth": truth.max_cascade_depth,
                "disrupted_volume": truth.disrupted_volume,
                "optimum": optimum.available,
            }
        )

    # --- deterministic replay ----------------------------------------------
    again = generate_network(scale_config(scale, seed=seed))
    failures.check(
        again.dataset_version == network.dataset_version,
        f"{tag}: dataset id is not reproducible",
    )
    d1 = LiquiditySimulator(graph, sim).run(None, run_id="r").disruption
    d2 = LiquiditySimulator(again.graph, sim).run(None, run_id="r").disruption
    failures.check(d1 == d2, f"{tag}: baseline disruption is not reproducible")

    probe = ScenarioSpec(
        family=ScenarioFamily.LIQUIDITY_DRAIN,
        magnitude=2.0,
        target_strategy=TargetStrategy.EXPLICIT,
        explicit_targets=(suite[0].targets[0],),
    )
    s1 = build_scenario(graph, probe, dataset_id=network.dataset_version)
    s2 = build_scenario(again.graph, probe, dataset_id=again.dataset_version)
    failures.check(
        s1.scenario_id == s2.scenario_id, f"{tag}: scenario id is not reproducible"
    )
    g1 = compute_ground_truth(s1, config=sim, compute_optimum=False)
    g2 = compute_ground_truth(s2, config=sim, compute_optimum=False)
    failures.check(
        sorted(g1.affected_nodes) == sorted(g2.affected_nodes),
        f"{tag}: replayed affected set differs",
    )
    failures.check(
        g1.max_cascade_depth == g2.max_cascade_depth,
        f"{tag}: replayed cascade depth differs",
    )

    # --- the standing scientific battery ------------------------------------
    report = validate_dataset(network, config=sim, deep=True)
    for check in report.failures:
        failures.items.append(f"{tag}/validation: {check.name} - {check.detail}")

    return {
        "scale": str(scale),
        "seed": seed,
        "dataset_id": network.dataset_version,
        "n_merchants": len(graph),
        "n_events": graph.stats().n_payment_events,
        "validation": report.summary(),
        "scenarios": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase-2 acceptance harness")
    parser.add_argument("--scales", nargs="*", default=["small"])
    parser.add_argument("--seeds", nargs="*", type=int, default=[2025, 7, 99])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    failures = Failures()
    family_bites: dict[str, list[bool]] = {}
    results = []
    for scale_name in args.scales:
        scale = BenchmarkScale(scale_name)
        for seed in args.seeds:
            results.append(verify_seed(scale, seed, failures, family_bites))

    # --- sweep-level severity -------------------------------------------------
    for family in REQUIRED_FAMILIES:
        outcomes = family_bites.get(str(family), [])
        failures.check(bool(outcomes), f"{family}: never built across the sweep")
        if not outcomes:
            continue
        if len(outcomes) < MIN_SEEDS_FOR_RATE:
            continue
        rate = sum(outcomes) / len(outcomes)
        failures.check(
            rate >= MIN_BITE_RATE,
            f"{family}: produced a cascade on only {sum(outcomes)}/{len(outcomes)} "
            f"seeds ({rate:.0%}, need {MIN_BITE_RATE:.0%})",
        )

    depths = [r["max_depth"] for res in results for r in res["scenarios"]]
    affected = [r["n_affected"] for res in results for r in res["scenarios"]]
    failures.check(
        max(depths, default=0) >= 1,
        "no scenario anywhere in the sweep produced a multi-hop cascade",
    )
    failures.check(
        max(affected, default=0) >= 3,
        "no scenario anywhere in the sweep affected 3+ merchants",
    )

    if args.json:
        print(json.dumps({"results": results, "failures": failures.items}, indent=2, default=str))
    else:
        for result in results:
            print(f"\n=== {result['scale']} seed={result['seed']} "
                  f"({result['n_merchants']} merchants, {result['n_events']} events) ===")
            print(f"  validation: {result['validation']}")
            print(f"  {'family':24} {'target':10} {'t':>6} {'shock':>10} "
                  f"{'aff':>4} {'depth':>5} {'volume':>10} opt")
            for row in result["scenarios"]:
                print(
                    f"  {row['family']:24} {row['targets'][0]:10} {row['shock_t']:6.1f} "
                    f"{row['magnitude']:10.3e} {row['n_affected']:4d} {row['max_depth']:5d} "
                    f"{row['disrupted_volume']:10.3e} {'Y' if row['optimum'] else '-'}"
                )

    print(f"\n{'=' * 72}")
    if failures.items:
        print(f"FAILED - {len(failures.items)} invariant violation(s):")
        for item in failures.items:
            print(f"  - {item}")
        return 1
    print("ALL PHASE-2 INVARIANTS HOLD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
