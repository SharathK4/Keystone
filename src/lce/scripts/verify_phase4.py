"""Phase-4 acceptance harness.

Checks the thirteen acceptance criteria directly, one assertion each, and exits
non-zero if any fails, so it is usable as a gate.

    python src/lce/scripts/verify_phase4.py --profile small_fast --seeds 2025 7

The medium-scale check is opt-in (``--medium``) because it generates a
thousand-merchant network; everything else is sized to finish on a laptop.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Runnable directly from a checkout as well as from an installed package.
# parents: [0] scripts, [1] lce, [2] src, [3] repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np

from lce.intervention.experiment import (
    Phase4Config,
    run_phase4,
    write_artifact,
)
from lce.intervention.problem import ObjectiveSpec
from lce.intervention.profiles import ResourceProfile, budget_for
from lce.logging import configure_logging


class Failures:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.passed: list[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed.append(name)
        else:
            self.items.append(f"{name}: {detail}" if detail else name)
        return condition


def _small_overrides() -> dict[str, object]:
    """A network small enough that the harness finishes in a couple of minutes."""
    return {"n_merchants": 40, "n_layers": 3, "history_hours": 20 * 24.0}


def verify_pipeline(args: argparse.Namespace, failures: Failures) -> dict:
    """Criteria 1-7: predict, rank, select, replay, measure, optimum, budget."""
    config = Phase4Config(
        profile=ResourceProfile(args.profile),
        seeds=tuple(args.seeds),
        objective=ObjectiveSpec(lam=args.lam),
        robust=True,
        systemic=True,
        pruning_benchmark=True,
    )
    started = time.perf_counter()
    report = run_phase4(config, generator_overrides=_small_overrides())
    elapsed = time.perf_counter() - started

    failures.check("1_shock_predicted", bool(report.scenarios), "no scenario ran")
    if not report.scenarios:
        return {}

    scenario = report.scenarios[0]
    failures.check(
        "1_prediction_from_observables",
        scenario.prediction.get("n_observed_events", 0) > 0
        and scenario.prediction.get("origin_t") is not None,
        "prediction carries no observable window",
    )
    failures.check(
        "2_ranked_interventions",
        scenario.candidates["n_retained"] > 0,
        "candidate set is empty",
    )
    failures.check(
        "2_candidates_are_explained",
        all(
            entry.get("factors") for entry in scenario.candidates["candidates"]
        ),
        "a candidate carries no measurable factors",
    )

    names = {o.name for s in report.scenarios for o in s.counterfactual.outcomes}
    failures.check(
        "3_recommendation_selected",
        "model_guided_greedy" in names,
        "no model-guided recommendation was produced",
    )
    failures.check(
        "4_replayed_in_the_simulator",
        all(
            o.replay_runtime_s >= 0.0 and o.baseline_disruption > 0.0
            for s in report.scenarios
            for o in s.counterfactual.outcomes
        ),
        "an outcome was not replayed",
    )
    failures.check(
        "5_before_and_after_reported",
        all(
            o.true_disruption >= 0.0 and o.baseline_disruption > 0.0
            for s in report.scenarios
            for o in s.counterfactual.outcomes
        ),
        "disruption before/after is missing",
    )

    budget = budget_for(args.profile)
    if budget.exact_optimum:
        with_optimum = [
            s for s in report.scenarios if s.counterfactual.by_name("exact_optimum")
        ]
        failures.check(
            "6_small_networks_have_a_true_optimum",
            bool(with_optimum),
            "no scenario produced an exact optimum",
        )
        # The reference must dominate every *feasible* strategy, or it is not an
        # optimum. Infeasible plans are excluded on purpose: the exact solver
        # searches only the feasible set, so a plan that breaches a constraint can
        # of course score better on the objective - that is what the constraint is
        # costing, not evidence the reference is wrong.
        dominated = True
        worst = 0.0
        for result in with_optimum:
            optimum = result.counterfactual.by_name("exact_optimum")
            if optimum is None:
                continue
            best = optimum.objective_value(config.objective)
            for outcome in result.counterfactual.outcomes:
                if outcome.violations:
                    continue
                gap = outcome.objective_value(config.objective) - best
                worst = min(worst, gap)
                if gap < -1e-6 * max(1.0, abs(best)):
                    dominated = False
        failures.check(
            "6_optimum_dominates_every_strategy",
            dominated,
            f"a feasible strategy beat the reference optimum by {abs(worst):,.2f}",
        )

    failures.check(
        "invariant_no_money_creation",
        all(
            "no_money_creation" not in o.violations
            for s in report.scenarios
            for o in s.counterfactual.outcomes
        ),
        "an action changed the obligated principal",
    )
    failures.check(
        "invariant_feasible_recommendations",
        all(
            not recommended.violations
            for recommended in (
                s.counterfactual.by_name("model_guided_greedy") for s in report.scenarios
            )
            if recommended is not None
        ),
        "a recommended action violated a constraint",
    )
    failures.check(
        "7_runs_within_the_laptop_budget",
        elapsed < args.max_seconds,
        f"took {elapsed:.0f}s (limit {args.max_seconds}s)",
    )

    path = write_artifact(report, root=Path(args.out))
    payload = json.loads(path.read_text(encoding="utf-8"))
    failures.check(
        "12_artifact_records_provenance",
        {"code_version", "config_hash", "seeds", "feature_schema_version"}
        <= set(payload["provenance"]),
        "the run artifact is missing provenance fields",
    )
    return {
        "run_id": report.run_id,
        "artifact": str(path),
        "elapsed_s": round(elapsed, 1),
        "n_scenarios": len(report.scenarios),
        "summary": report.summary(),
        "pruning": [
            s.pruning for s in report.scenarios if s.pruning.get("n_before")
        ],
        "systemic": report.systemic,
    }


def verify_reproducibility(args: argparse.Namespace, failures: Failures) -> None:
    """Criterion 12: the same seed and config reproduce the same decision."""
    from lce.benchmark.scenarios import ScenarioFamily

    config = Phase4Config(
        profile=ResourceProfile.SMALL_FAST,
        seeds=(args.seeds[0],),
        families=(ScenarioFamily.CONCENTRATED_SHOCK,),
        robust=False,
        systemic=False,
        pruning_benchmark=False,
    )
    first = run_phase4(config, generator_overrides=_small_overrides())
    second = run_phase4(config, generator_overrides=_small_overrides())

    def decision(report):
        outcome = report.scenarios[0].counterfactual.by_name("model_guided_greedy")
        return (
            [(str(u.type), u.merchant_id, round(u.amount, 6)) for u in outcome.interventions],
            round(outcome.true_disruption, 6),
        )

    failures.check(
        "12_decision_reproduces",
        decision(first) == decision(second),
        f"{decision(first)} != {decision(second)}",
    )
    failures.check(
        "12_config_hash_stable",
        first.run_id == second.run_id,
        f"{first.run_id} != {second.run_id}",
    )


def verify_inference(args: argparse.Namespace, failures: Failures) -> dict:
    """Criteria 8-10: export a model, load it cleanly, serve REST calls."""
    from lce.inference.artifact import load_artifact
    from lce.inference.export import export_hazard_model
    from lce.learning.baselines import DiscreteTimeHazard
    from lce.learning.dataset import build_corpus
    from lce.learning.experiment import fit_and_calibrate
    from lce.learning.features import FEATURE_SCHEMA_VERSION
    from lce.learning.splits import SplitSpec, assert_split_clean, make_temporal_split

    workdir = Path(tempfile.mkdtemp(prefix="phase4-accept-"))
    try:
        overrides = {
            "n_merchants": 22,
            "n_layers": 3,
            "history_hours": 15 * 24.0,
            "horizon_hours": 168.0,
        }
        corpus = build_corpus([61, 62, 63, 64], overrides=overrides, audit=False)
        split = make_temporal_split(corpus, SplitSpec(0.5, 0.25))
        assert_split_clean(corpus, split)
        fitted = fit_and_calibrate(
            "discrete_hazard",
            DiscreteTimeHazard(corpus.task),
            split.examples(corpus, "train"),
            split.examples(corpus, "validation"),
        )
        assert isinstance(fitted.model, DiscreteTimeHazard)
        directory = export_hazard_model(
            fitted.model,
            workdir / "artifacts" / "accept",
            calibrator=fitted.calibrator,
            threshold=fitted.threshold,
            dataset_version=corpus.dataset_ids()[0],
            seeds=[61, 62, 63, 64],
        )
        failures.check("8_model_exports", directory.exists())

        artifact = load_artifact(directory, expected_schema=FEATURE_SCHEMA_VERSION)
        failures.check(
            "8_artifact_is_self_describing",
            bool(artifact.manifest.content_hash) and bool(artifact.manifest.model_version),
        )

        # Criterion 9: a clean process, importing only the inference package.
        code = (
            "import json, sys\n"
            "from lce.inference.service import InferenceService, shock_from_components\n"
            "from lce.inference.predictor import NetworkState, merchant_from_payload\n"
            "from lce.domain.events import Obligation, PaymentEvent\n"
            f"service = InferenceService({str(directory.parent)!r})\n"
            "state = NetworkState(network_id='clean',\n"
            "  merchants=[merchant_from_payload({'merchant_id': f'm{i}',"
            " 'opening_balance': 100000.0}) for i in range(3)],\n"
            "  obligations=[Obligation(obligation_id='o0', debtor_id='m0',"
            " creditor_id='m1', amount=50000.0, issued_t=-24.0, due_t=24.0)],\n"
            "  payments=[PaymentEvent(payer_id='m0', payee_id='m1', amount=10000.0, t=-12.0)])\n"
            "shock = shock_from_components([{'merchant_id': 'm0',"
            " 'magnitude': 40000.0, 't': 0.0}])\n"
            "p, _ = service.predict_contagion(state, shock, observation_cutoff=0.0,"
            " horizon_hours=168.0)\n"
            "forbidden = ('lce.data.generator','lce.benchmark','lce.learning.dataset')\n"
            "leaked = sorted(m for m in sys.modules if any(m == f or"
            " m.startswith(f + '.') for f in forbidden))\n"
            "print(json.dumps({'n': len(p.nodes), 'leaked': leaked,"
            " 'version': p.model_version}))\n"
        )
        import os

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[3],
            env={**os.environ, "PYTHONPATH": "src", "LCE_LOG_LEVEL": "ERROR"},
        )
        clean_ok = result.returncode == 0
        payload = json.loads(result.stdout.strip().splitlines()[-1]) if clean_ok else {}
        failures.check(
            "9_clean_process_loads_the_artifact",
            clean_ok and payload.get("n") == 3,
            result.stderr[-400:] if not clean_ok else "",
        )
        failures.check(
            "9_no_training_code_on_the_serving_path",
            payload.get("leaked") == [],
            f"leaked modules: {payload.get('leaked')}",
        )

        # Criterion 10: REST.
        from fastapi.testclient import TestClient

        from lce.api.app import create_app
        from lce.inference import service as service_module
        from lce.inference.service import InferenceService

        original = service_module.get_service
        service_module.get_service = lambda *a, **k: InferenceService(directory.parent)
        try:
            with TestClient(create_app()) as client:
                request = _rest_payload()
                predict = client.post("/api/v1/predict/contagion", json=request)
                failures.check(
                    "10_rest_predict",
                    predict.status_code == 200 and len(predict.json()["nodes"]) == 3,
                    predict.text[:300],
                )
                recommend = client.post(
                    "/api/v1/interventions/recommend",
                    json=request | {"constraints": {"max_actions": 1}, "max_candidates": 4},
                )
                failures.check(
                    "10_rest_recommend",
                    recommend.status_code == 200,
                    recommend.text[:300],
                )
                replay_payload = {k: v for k, v in request.items() if k != "observation_cutoff"}
                replay_payload["interventions"] = [
                    {
                        "type": "liquidity_injection",
                        "merchant_id": "m0",
                        "t": 0.0,
                        "amount": 90000.0,
                    }
                ]
                replayed = client.post("/api/v1/scenarios/replay", json=replay_payload)
                failures.check(
                    "10_rest_replay",
                    replayed.status_code == 200
                    and replayed.json()["n_interventions"] == 1,
                    replayed.text[:300],
                )
                bad = client.post("/api/v1/predict/contagion", json={"network": {}})
                failures.check(
                    "10_rest_rejects_malformed", bad.status_code == 422, bad.text[:200]
                )
        finally:
            service_module.get_service = original

        return {
            "artifact": str(directory),
            "model_version": artifact.manifest.model_version,
            "calibrator": artifact.manifest.calibrator.get("calibrator"),
            "content_hash": artifact.manifest.content_hash[:16],
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _rest_payload() -> dict:
    return {
        "network": {
            "network_id": "accept",
            "merchants": [
                {"merchant_id": "m0", "opening_balance": 100000.0, "credit_limit": 20000.0},
                {"merchant_id": "m1", "opening_balance": 200000.0, "credit_limit": 20000.0},
                {"merchant_id": "m2", "opening_balance": 300000.0, "credit_limit": 20000.0},
            ],
            "obligations": [
                {
                    "obligation_id": "obl_0", "debtor_id": "m0", "creditor_id": "m1",
                    "amount": 60000.0, "issued_t": -48.0, "due_t": 24.0,
                },
                {
                    "obligation_id": "obl_1", "debtor_id": "m1", "creditor_id": "m2",
                    "amount": 60000.0, "issued_t": -48.0, "due_t": 36.0,
                },
            ],
            "payments": [
                {"payer_id": "m0", "payee_id": "m1", "amount": 30000.0, "t": -30.0},
                {"payer_id": "m1", "payee_id": "m2", "amount": 20000.0, "t": -28.0},
            ],
        },
        "shock": {"components": [{"merchant_id": "m0", "magnitude": 90000.0, "t": 0.0}]},
        "observation_cutoff": 0.0,
        "horizon_hours": 168.0,
    }


def verify_razorpay(failures: Failures) -> dict:
    """Criterion 11: the provider boundary is isolated and testable."""
    from lce.config import RazorpayMode, RazorpaySettings
    from lce.domain.enums import InterventionType
    from lce.domain.intervention import Intervention
    from lce.errors import ConfigError
    from lce.execution import RazorpayTestProvider, SimulationProvider

    action = Intervention(
        type=InterventionType.LIQUIDITY_INJECTION,
        merchant_id="m0", t=0.0, amount=1234.56,
    )

    failures.check(
        "11_simulation_provider_works",
        SimulationProvider().execute(action).status == "planned",
    )

    refused = False
    try:
        RazorpayTestProvider(settings=RazorpaySettings(RAZORPAY_MODE=RazorpayMode.LIVE))
    except ConfigError:
        refused = True
    failures.check("11_live_mode_is_refused", refused, "live mode was accepted")

    provider = RazorpayTestProvider()
    record = provider.execute(action)
    failures.check(
        "11_no_funds_move",
        record.status == "planned",
        f"provider reported {record.status}",
    )
    mapped = provider.map_to_request(action)
    failures.check(
        "11_amounts_are_paise", mapped["body"]["amount"] == 123456, str(mapped)
    )
    blob = json.dumps(record.to_dict())
    failures.check(
        "11_no_secret_in_output",
        "key_secret" not in blob and "rzp_" not in blob,
        "a credential appeared in a provider record",
    )
    return {"capabilities": provider.capabilities()}


def verify_medium(failures: Failures) -> dict:
    """Criterion 7 at MEDIUM: a thousand merchants inside the laptop budget."""
    from lce.benchmark.scenarios import ScenarioFamily

    config = Phase4Config(
        profile=ResourceProfile.MEDIUM,
        seeds=(2025,),
        families=(ScenarioFamily.CONCENTRATED_SHOCK,),
        robust=False,
        systemic=False,
        pruning_benchmark=False,
    )
    started = time.perf_counter()
    report = run_phase4(config)
    elapsed = time.perf_counter() - started
    failures.check(
        "7_medium_runs_on_a_laptop",
        bool(report.scenarios) and elapsed < 900.0,
        f"took {elapsed:.0f}s",
    )
    return {"elapsed_s": round(elapsed, 1), "n_scenarios": len(report.scenarios)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase-4 acceptance harness")
    parser.add_argument(
        "--profile", default="small_fast", choices=[p.value for p in ResourceProfile]
    )
    parser.add_argument("--seeds", nargs="*", type=int, default=[2025, 7])
    parser.add_argument("--lam", type=float, default=1.0)
    parser.add_argument("--out", default="reports/phase4")
    parser.add_argument("--max-seconds", type=float, default=600.0, dest="max_seconds")
    parser.add_argument("--medium", action="store_true", help="Also run the MEDIUM check.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args()

    configure_logging(level=args.log_level, force=True)
    failures = Failures()

    pipeline = verify_pipeline(args, failures)
    verify_reproducibility(args, failures)
    inference = verify_inference(args, failures)
    razorpay = verify_razorpay(failures)
    medium = verify_medium(failures) if args.medium else {}

    payload = {
        "pipeline": pipeline,
        "inference": inference,
        "razorpay": razorpay,
        "medium": medium,
        "passed": failures.passed,
        "failed": failures.items,
    }

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _report(payload, failures)

    print(f"\n{'=' * 72}")
    if failures.items:
        print(f"FAILED - {len(failures.items)} acceptance criterion violation(s):")
        for item in failures.items:
            print(f"  - {item}")
        return 1
    print(f"ALL PHASE-4 ACCEPTANCE CRITERIA HOLD ({len(failures.passed)} checks)")
    return 0


def _report(payload: dict, failures: Failures) -> None:
    pipeline = payload.get("pipeline") or {}
    if pipeline:
        print(f"\nrun_id            {pipeline['run_id']}")
        print(f"scenarios         {pipeline['n_scenarios']} in {pipeline['elapsed_s']}s")
        print(f"artifact          {pipeline['artifact']}")

        print(
            f"\n{'strategy':30} {'red%':>7} {'cost':>14} {'cap.eff':>9} "
            f"{'rel.gap':>8} {'infeas':>6}"
        )
        rows = pipeline["summary"]["strategies"]
        for row in sorted(rows, key=lambda r: -(r.get("median_reduction_pct") or 0.0)):
            print(
                f"{row['strategy']:30} {_f(row['mean_reduction_pct'], 7, 2)} "
                f"{_f(row['mean_cost'], 14, 0)} "
                f"{_f(row['median_capital_efficiency'], 9, 2)} "
                f"{_f(row['mean_relative_gap'], 8, 4)} {row['n_infeasible']:6d}"
            )

        for entry in pipeline.get("pruning", []):
            print(
                f"\npruning           {entry['n_before']} -> {entry['n_after']} candidates, "
                f"optimum retained={entry['optimum_retained']}, "
                f"relative regret={_f(entry.get('relative_regret'), 0, 5)}, "
                f"runtime -{_f((entry.get('runtime_reduction') or 0) * 100, 0, 1)}%"
            )

        for dataset, systemic in (pipeline.get("systemic") or {}).items():
            correlations = systemic.get("baseline_rank_correlation", {})
            print(
                f"\nsystemic ({dataset[:18]}): rank correlation with "
                f"throughput={_f(correlations.get('throughput'), 0, 2)}, "
                f"degree={_f(correlations.get('degree'), 0, 2)}, "
                f"deficit={_f(correlations.get('cash_deficit'), 0, 2)}"
            )

    if payload.get("inference"):
        info = payload["inference"]
        print(
            f"\ninference         {info['model_version']} "
            f"(calibrator={info['calibrator']}, hash={info['content_hash']})"
        )
    if payload.get("medium"):
        print(f"medium            {payload['medium']}")
    print(f"\nchecks passed     {len(failures.passed)}")


def _f(value: object, width: int, places: int) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and np.isfinite(value):
        return f"{value:{width}.{places}f}"
    return "-".rjust(width) if width else "-"


if __name__ == "__main__":
    raise SystemExit(main())
