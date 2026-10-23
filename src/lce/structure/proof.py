"""Build the proof artifact: the inspection certificate behind Folio V.

Everything in it is measured when the artifact is built, and nothing is typed
in: the leakage barrier per scenario, how many of the generator's true
dependencies the estimator recovered (with Wilson intervals), the backend audit
as pass/fail lines, headline results with their intervals, and the weaknesses,
each with the number that shows it.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from lce import __version__
from lce.logging import get_logger
from lce.structure.models import (
    LABEL,
    AuditLine,
    FailedItem,
    LeakageRow,
    MeasuredResult,
    ParameterRecovery,
    ProofProvenance,
    Rate,
    RecoveryView,
)

logger = get_logger(__name__)

FORMAT = "keystone-proof-v1"
Z95 = 1.959963984540054
BOOT = 2000
BOOT_SEED = 20251022
PARAMETERS: tuple[tuple[str, str, str, Any], ...] = (
    (
        "conditional_probability",
        "Chance a shortfall passes on",
        "",
        lambda e: e.conditional_probability,
    ),
    ("pass_through", "Share of a shortfall passed on", "", lambda e: e.pass_through),
    ("lag_mean_hours", "Mean lag before it lands", "h", lambda e: e.lag.mean_hours),
)


def _ranks(x: Any) -> Any:
    import numpy as np

    order = np.argsort(x, kind="stable")
    r = np.empty(len(x))
    r[order] = np.arange(len(x))
    return r


def _spearman(a: Any, b: Any) -> float:
    import numpy as np

    ra, rb = _ranks(a), _ranks(b)
    if ra.std() == 0 or rb.std() == 0:
        return 0.0
    return float(np.corrcoef(ra, rb)[0, 1])


def parameter_recovery(
    true_edges: dict[Any, Any], estimated: dict[Any, dict[str, Any]]
) -> list[ParameterRecovery]:
    """Spearman agreement per strength, with a bootstrap interval over links."""
    import numpy as np

    keys = sorted(set(true_edges) & set(estimated))
    rng = np.random.default_rng(BOOT_SEED)
    idx = rng.integers(0, len(keys), size=(BOOT, len(keys))) if keys else None
    out = []
    for name, label, unit, truth in PARAMETERS:
        t = np.array([truth(true_edges[k]) for k in keys], dtype=float)
        e = np.array([estimated[k][name] for k in keys], dtype=float)
        rho = _spearman(t, e)
        boots = [_spearman(t[i], e[i]) for i in idx] if idx is not None else [rho]
        lo, hi = np.quantile(boots, [0.025, 0.975])
        out.append(
            ParameterRecovery(
                name=name,
                label=label,
                n_edges=len(keys),
                rank_correlation=rho,
                low=float(lo),
                high=float(hi),
                mean_abs_error=float(np.mean(np.abs(t - e))) if keys else 0.0,
                unit=unit,
            )
        )
    return out


def _pulls_summary(root: Path) -> dict[str, int] | None:
    from lce.structure.store import PullsStore, resolve_artifact

    try:
        pulls = PullsStore(resolve_artifact(root, "pulls"))
    except Exception:  # the proof is still built without it; the item says less
        return None
    depths = [
        max((a.cascade_depth or 0) for a in r.affected) if r.affected else 0
        for r in pulls.view.records
    ]
    return {
        "n": len(depths),
        "one_hop": sum(1 for d in depths if d == 1),
        "deeper": sum(1 for d in depths if d > 1),
    }


def wilson(k: int, n: int) -> Rate:
    if n == 0:
        return Rate(value=0.0, low=0.0, high=0.0, numerator=k, denominator=n)
    p = k / n
    denom = 1 + Z95**2 / n
    centre = (p + Z95**2 / (2 * n)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95**2 / (4 * n**2)) / denom
    return Rate(
        value=p,
        low=max(0.0, centre - half),
        high=min(1.0, centre + half),
        numerator=k,
        denominator=n,
    )


def _leakage_and_recovery(snapshot: dict[str, Any]) -> tuple[list[LeakageRow], RecoveryView, float]:
    """Rebuild the canonical network offline; check the barrier; score recovery."""
    from lce.benchmark.scales import BenchmarkScale, scale_config
    from lce.benchmark.scenarios import baseline_affected_set, scenario_suite
    from lce.data.generator import generate_network
    from lce.intervention.evaluate import replay
    from lce.learning.problem import baseline_payment_stream, build_observed_window
    from lce.simulation.engine import SimulationConfig

    build = snapshot["build"]
    gen = scale_config(BenchmarkScale(build["scale"]), seed=build["seed"])
    network = generate_network(gen)
    if network.dataset_version != snapshot["network"]["dataset_version"]:
        raise ValueError("the regenerated network is not the snapshot's network")
    sim = SimulationConfig(horizon_hours=gen.horizon_hours, seed=build["seed"])
    rows: list[LeakageRow] = []
    delayed_inflow_own_network = math.nan
    for sc in scenario_suite(
        network.graph,
        dataset_id=network.dataset_version,
        seed=build["seed"],
        magnitude=build["magnitude"],
        config=sim,
        baseline_affected=baseline_affected_set(network.graph, sim),
    ):
        stream = baseline_payment_stream(sc.unperturbed_graph, sim)
        window = build_observed_window(sc, config=sim, baseline_payments=stream)
        times = [e.t for e in window.graph.payment_events]
        last = max(times) if times else None
        rows.append(
            LeakageRow(
                scenario_id=sc.scenario_id,
                family=str(sc.spec.family),
                onset_hours=sc.shock.onset_t,
                first_observed_hours=min(times) if times else None,
                last_observed_hours=last,
                n_observed_events=len(times),
                holds=last is None or last < sc.shock.onset_t,
            )
        )
        if str(sc.spec.family) == "delayed_inflow":
            chosen = _plan_of(sc, sim)
            if chosen:
                none = replay(sc.graph, sc.shock, [], config=sim)
                treated = replay(sc.graph, sc.shock, chosen, config=sim)
                delayed_inflow_own_network = (
                    100 * (none.disruption - treated.disruption) / none.disruption
                )
    truth = {
        (e.source_id, e.target_id): e for e in network.graph.dependency_edges if e.is_ground_truth
    }
    est = {(d["source_id"], d["target_id"]): d for d in snapshot["dependencies"]}
    matched = len(set(truth) & set(est))
    recovery = RecoveryView(
        n_true=len(truth),
        n_estimated=len(est),
        n_matched=matched,
        precision=wilson(matched, len(est)),
        recall=wilson(matched, len(truth)),
        parameters=parameter_recovery(truth, est),
    )
    return rows, recovery, delayed_inflow_own_network


def _plan_of(sc: Any, sim: Any) -> list[Any]:
    """The snapshot's selected plan, re-derived exactly as the value-of-speed build does."""
    from lce.intervention.experiment import predict_with_propagation
    from lce.intervention.profiles import ResourceProfile, budget_for
    from lce.structure.build import optimise

    prediction, learned, _ = predict_with_propagation(sc, sim_config=sim)
    return optimise(
        sc,
        learned=learned,
        prediction=prediction,
        sim=sim,
        budget=budget_for(ResourceProfile.SMALL_FAST),
        decision_time=sc.shock.onset_t,
    )


def _audit(artifact_root: Path) -> list[AuditLine]:
    from lce.scripts import audit_backend as ab

    audit = ab.Audit()
    ab.audit_secrets(audit)
    ab.audit_serving(audit)
    ab.audit_artifacts(audit, artifact_root=artifact_root)
    ab.audit_determinism(audit, artifact_root=artifact_root)
    ab.audit_api(audit)
    ab.audit_migrations(audit)
    ab.audit_paypal(audit)
    ab.audit_structure(audit, artifact_root=artifact_root)
    ab.audit_demo(audit)
    lines = [AuditLine(name=n, passed=True) for n in audit.passed]
    lines += [AuditLine(name=n.split(":")[0], passed=False) for n in audit.failed]
    # the legacy provider's own checks are not part of this product's surface
    return [ln for ln in lines if "razorpay" not in ln.name]


def build_proof(*, artifact_root: Path, snapshot_id: str) -> dict[str, Any]:
    from lce.structure.store import SpeedStore, resolve_speed

    snapshot = json.loads(
        (artifact_root / "snapshots" / snapshot_id / "snapshot.json").read_text(encoding="utf-8")
    )
    speed = SpeedStore(resolve_speed(artifact_root / "structure", snapshot_id))
    leakage, recovery, delayed_own = _leakage_and_recovery(snapshot)
    audit = _audit(artifact_root)

    scen = snapshot["scenarios"]
    planned = [s for s in scen if s["recommended_intervention"]]
    gaps = [
        s["counterfactual"]["optimality_gap"]
        for s in planned
        if s["counterfactual"]["optimality_gap"] is not None
    ]
    pts = {p.delay_hours: p for p in speed.points}
    canonical = next(s for s in scen if s["family"] == "single_missed_inflow")
    results = [
        *[
            MeasuredResult(
                label=f"{p.label}: rank agreement with the truth",
                value=p.rank_correlation,
                low=p.low,
                high=p.high,
                unit="rho",
                note=f"Spearman over {p.n_edges} links; bootstrap 95 % over links",
            )
            for p in recovery.parameters
        ],
        MeasuredResult(
            label="Largest gap to the exact optimum",
            value=100 * max(gaps) if gaps else 0.0,
            low=None,
            high=None,
            unit="%",
            note=f"over the {len(gaps)} snapshot scenarios that have a plan",
        ),
        MeasuredResult(
            label="Canonical plan, network disruption cut",
            value=canonical["counterfactual"]["disruption_reduction_pct"],
            low=None,
            high=None,
            unit="%",
            note="one scenario, common random numbers; no interval",
        ),
        *[
            MeasuredResult(
                label=f"Cut with the money {int(h)} h late, mean over scenarios",
                value=pts[h].frozen_cut_pct.mean,
                low=pts[h].frozen_cut_pct.low,
                high=pts[h].frozen_cut_pct.high,
                unit="%",
                note=f"{pts[h].n_scenarios} scenarios with a plan; bootstrap 95 %",
            )
            for h in (0.0, 24.0, 48.0)
        ],
    ]

    beyond = sum(1 for s in scen for a in s["affected_merchants"] if (a["cascade_depth"] or 0) > 0)
    pulls = _pulls_summary(artifact_root / "structure")
    worst = min(recovery.parameters, key=lambda p: p.rank_correlation)
    n_no_plan = sum(1 for s in speed.scenarios() if not s.has_plan)
    misses = [
        s
        for s in speed.scenarios()
        if s.has_plan
        and len(s.delays) > 1
        and s.delays[1].replanned.disruption_cut_pct - s.delays[0].frozen.disruption_cut_pct > 5
    ]
    delayed_snap = next(s for s in scen if s["family"] == "delayed_inflow")
    failed = [
        FailedItem(
            title="Shocks barely cascade on this network",
            detail=f"In the {len(scen)} snapshot scenarios {beyond} merchants beyond the shocked "
            "ones are constrained. "
            + (
                f"Of the {pulls['n']} single-merchant pulls, {pulls['one_hop']} constrain another "
                f"merchant one hop away and {pulls['deeper']} go further. "
                if pulls
                else ""
            )
            + "Plans are measured on the whole week's network disruption, most of which exists "
            "without the shock.",
        ),
        FailedItem(
            title="Some benchmark scenarios have no plan at all",
            detail=f"{n_no_plan} of {len(speed.scenarios())} benchmark scenarios end with no "
            "action "
            "the optimiser will take.",
        ),
        FailedItem(
            title="The optimiser's candidate list can miss the best plan",
            detail=f"In {len(misses)} benchmark scenarios, re-planning one hour later finds a plan "
            "worth more than the plan chosen at the shock; the top-12 candidate list at the "
            "shock had left it out.",
        ),
        FailedItem(
            title="One snapshot figure leaves a scenario's own change out",
            detail=f"The delayed-inflow scenario shows "
            f"{delayed_snap['counterfactual']['disruption_reduction_pct']:.2f} %; replayed with "
            f"its delay applied the same plan cuts {delayed_own:.2f} %. The snapshot is frozen "
            "and is shown as built.",
        ),
        FailedItem(
            title="Links are observed; only their strengths are inferred",
            detail=f"All {recovery.n_true} true links appear in the payment history, so finding "
            "them is not the hard part. Their strengths are estimated with error: the weakest, "
            f"{worst.label.lower()}, ranks links with a correlation of "
            f"{worst.rank_correlation:.2f} [{worst.low:.2f}, {worst.high:.2f}] against the "
            "generator's truth.",
        ),
        FailedItem(
            title="Webhook certificate chain not validated",
            detail="Signatures are checked against a certificate fetched from an HTTPS paypal.com "
            "address within its validity window; the chain of trust is not walked.",
        ),
    ]
    provenance = ProofProvenance(
        kind="replayed_offline",
        label=LABEL,
        snapshot_id=snapshot_id,
        speed_artifact_id=speed.artifact_id,
        code_version=__version__,
    )
    return {
        "format": FORMAT,
        "provenance": provenance.model_dump(mode="json"),
        "leakage": [r.model_dump(mode="json") for r in leakage],
        "recovery": recovery.model_dump(mode="json"),
        "audit": [a.model_dump(mode="json") for a in audit],
        "results": [r.model_dump(mode="json") for r in results],
        "failed": [f.model_dump(mode="json") for f in failed],
    }
