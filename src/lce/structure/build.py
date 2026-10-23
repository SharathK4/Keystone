"""Build the value-of-speed artifact. Offline only; never imported by serving.

For every scenario and every delay ``d`` on the grid, two plans are replayed
against the same shock with the same random numbers:

* **frozen** - the plan chosen when the shock became known, with every action
  landing ``d`` hours later than it was scheduled;
* **replanned** - the existing optimiser run again, as if the earliest the
  money could land were ``d`` hours after the shock became known.

Each is compared with doing nothing. The difference between the two arms is
what replanning recovers; what neither recovers is the price of the delay.

At ``d = 0`` both arms are the plan the snapshot recommends, so the frozen arm
must reproduce the snapshot's counterfactual exactly. The build refuses to
write an artifact if it does not.

Plans are chosen exactly as the snapshot chooses them (on the scenario's own
network), and replayed exactly as the snapshot replays its counterfactual: the
scenario's shock on the unmutated network. For the two families that are
network mutations (delayed inflow, supplier failure) that means the mutation is
not part of the replay; ``REPLAY_NOTE`` says so in every artifact.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from lce import __version__
from lce.benchmark.scales import BenchmarkScale, scale_config
from lce.benchmark.scenarios import (
    BuiltScenario,
    ScenarioFamily,
    baseline_affected_set,
    scenario_suite,
)
from lce.config import ObjectiveSettings
from lce.data.generator import generate_network
from lce.domain.intervention import Intervention
from lce.errors import ValidationError
from lce.intervention.actions import generate_actions
from lce.intervention.evaluate import Replay, replay
from lce.intervention.experiment import predict_with_propagation
from lce.intervention.problem import ObjectiveSpec
from lce.intervention.profiles import ResourceBudget, ResourceProfile, budget_for
from lce.intervention.scalable import greedy_solve
from lce.logging import get_logger
from lce.seeds import config_hash
from lce.simulation.counterfactual import CounterfactualEvaluator
from lce.simulation.engine import SimulationConfig
from lce.structure.models import (
    DELAY_NOTE,
    LABEL,
    ArmResult,
    BootstrapSpec,
    DelayResult,
    Interval,
    Monotonicity,
    ObjectiveTerms,
    ScenarioSpeed,
    SpeedAction,
    SpeedPoint,
    SpeedProvenance,
)

logger = get_logger(__name__)

FORMAT = "keystone-speed-v1"
#: The Gate-0 probe grid.
DELAYS_HOURS: tuple[float, ...] = (0.0, 1.0, 6.0, 12.0, 24.0, 48.0, 72.0)
#: The Phase-4 benchmark's two networks: seven families each, fourteen scenarios.
SEEDS: tuple[int, ...] = (2025, 7)
MAGNITUDE = 2.0
BOOTSTRAP = BootstrapSpec(resamples=2000, seed=20251022, level=0.95, unit="scenario")
REPLAY_NOTE = (
    "Each plan is replayed with the scenario's shock on the unmutated network, the "
    "same replay the snapshot's counterfactual uses, so a zero delay reproduces the "
    "snapshot exactly. For network-mutation families the mutation is not replayed."
)
#: An action cannot land at or after the horizon; the last instant before it.
_LAST_INSTANT = 1e-6


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ one plan


def optimise(
    scenario: BuiltScenario,
    *,
    learned: Any,
    prediction: Any,
    sim: SimulationConfig,
    budget: ResourceBudget,
    decision_time: float,
) -> list[Intervention]:
    """The snapshot's optimiser, with the earliest landing moved to ``decision_time``."""
    if decision_time >= sim.horizon_hours - 1.0:
        return []
    constraints = replace(
        budget.constraints(), decision_time=decision_time, horizon_hours=sim.horizon_hours
    )
    action_set = generate_actions(
        learned, scenario.shock, prediction,
        constraints=constraints, max_candidates=budget.max_candidates,
    )
    evaluator = CounterfactualEvaluator(graph=scenario.graph, shock=scenario.shock, config=sim)
    solved = greedy_solve(
        evaluator, action_set.interventions, scenario.graph,
        constraints=constraints, objective=ObjectiveSpec(),
    )
    return list(solved.interventions)


def _terms(r: Replay) -> dict[str, float]:
    b = r.cascade.disruption_breakdown
    return {"delay": b.get("delay_term", 0.0), "default": b.get("default_term", 0.0),
            "deficit": b.get("deficit_term", 0.0), "total": r.disruption}


def arm(
    scenario: BuiltScenario, actions: Sequence[Intervention], *, network: Any,
    sim: SimulationConfig, unplanned: Replay, run_id: str,
) -> ArmResult:
    treated = (replay(network, scenario.shock, list(actions), config=sim, run_id=run_id)
               if actions else unplanned)
    before, after = _terms(unplanned), _terms(treated)
    prevented = unplanned.disruption - treated.disruption
    return ArmResult(
        actions=[
            SpeedAction(action_type=str(u.type), merchant_id=u.merchant_id, lands_at_hours=u.t,
                        amount_inr=u.amount, shift_hours=u.shift_hours, cost_inr=u.cost)
            for u in actions
        ],
        cost_inr=sum(u.cost for u in actions),
        disruption_index=treated.disruption,
        disruption_cut_pct=(100.0 * prevented / unplanned.disruption
                            if unplanned.disruption > 0 else 0.0),
        payments_kept_on_time_inr=unplanned.value_delayed - treated.value_delayed,
        merchants_protected=max(0, unplanned.n_affected - treated.n_affected),
        defaults_avoided=unplanned.n_defaulted - treated.n_defaulted,
        reduction=ObjectiveTerms(**{k: before[k] - after[k] for k in before}),
    )


def _rises(label: str, delays: Sequence[float], cuts: Sequence[float]) -> list[str]:
    return [
        f"{label}: {delays[i]:g} h {cuts[i]:.2f}% -> {delays[i + 1]:g} h {cuts[i + 1]:.2f}%"
        for i in range(len(cuts) - 1)
        if cuts[i + 1] > cuts[i] + 1e-9
    ]


def scenario_speed(
    scenario: BuiltScenario,
    *,
    network: Any,
    seed: int,
    sim: SimulationConfig,
    budget: ResourceBudget,
    delays: Sequence[float],
    in_snapshot: bool,
) -> ScenarioSpeed:
    prediction, learned, _meta = predict_with_propagation(scenario, sim_config=sim)
    onset = scenario.shock.onset_t
    horizon = sim.horizon_hours
    unplanned = replay(network, scenario.shock, [], config=sim, run_id="speed:none")

    chosen = optimise(scenario, learned=learned, prediction=prediction, sim=sim,
                      budget=budget, decision_time=onset)
    results: list[DelayResult] = []
    for d in delays:
        moved = [u.model_copy(update={"t": min(u.t + d, horizon - _LAST_INSTANT)})
                 for u in chosen]
        replanned = chosen if d == 0 else optimise(
            scenario, learned=learned, prediction=prediction, sim=sim, budget=budget,
            decision_time=onset + d,
        )
        results.append(DelayResult(
            delay_hours=d,
            frozen=arm(scenario, moved, network=network, sim=sim, unplanned=unplanned,
                       run_id=f"speed:f{d:g}"),
            replanned=arm(scenario, replanned, network=network, sim=sim, unplanned=unplanned,
                          run_id=f"speed:r{d:g}"),
        ))

    frozen_cuts = [r.frozen.disruption_cut_pct for r in results]
    replanned_cuts = [r.replanned.disruption_cut_pct for r in results]
    rises = _rises("frozen", delays, frozen_cuts) + _rises("replanned", delays, replanned_cuts)
    base = _terms(unplanned)
    return ScenarioSpeed(
        scenario_id=scenario.scenario_id,
        family=str(scenario.spec.family),
        seed=seed,
        dataset_version=scenario.dataset_id,
        shock_onset_hours=onset,
        in_snapshot=in_snapshot,
        has_plan=bool(chosen),
        unplanned=ObjectiveTerms(**base),
        payments_late_inr=unplanned.value_delayed,
        merchants_affected=unplanned.n_affected,
        defaults=unplanned.n_defaulted,
        delays=results,
        monotonicity=Monotonicity(
            frozen_non_increasing=not _rises("f", delays, frozen_cuts),
            replanned_non_increasing=not _rises("r", delays, replanned_cuts),
            rises=rises,
        ),
    )


# ---------------------------------------------------------------- aggregate


def _interval(values: np.ndarray, idx: np.ndarray, level: float) -> Interval:
    means = values[idx].mean(axis=1)
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(means, [tail, 1.0 - tail])
    return Interval(mean=float(values.mean()), low=float(low), high=float(high))


def aggregate(
    scenarios: Sequence[ScenarioSpeed], delays: Sequence[float], spec: BootstrapSpec = BOOTSTRAP
) -> list[SpeedPoint]:
    """Mean over scenarios that have a plan, each with a paired bootstrap interval."""
    planned = [s for s in scenarios if s.has_plan]
    if not planned:
        return []
    rng = np.random.default_rng(spec.seed)
    idx = rng.integers(0, len(planned), size=(spec.resamples, len(planned)))
    points = []
    for i, d in enumerate(delays):
        at = [s.delays[i] for s in planned]
        frozen = np.array([r.frozen.disruption_cut_pct for r in at])
        replanned = np.array([r.replanned.disruption_cut_pct for r in at])
        mix = Counter(a.action_type for r in at for a in r.replanned.actions)
        points.append(SpeedPoint(
            delay_hours=d,
            n_scenarios=len(planned),
            frozen_cut_pct=_interval(frozen, idx, spec.level),
            replanned_cut_pct=_interval(replanned, idx, spec.level),
            replanning_gain_pp=_interval(replanned - frozen, idx, spec.level),
            frozen_kept_inr=_interval(
                np.array([r.frozen.payments_kept_on_time_inr for r in at]), idx, spec.level),
            replanned_kept_inr=_interval(
                np.array([r.replanned.payments_kept_on_time_inr for r in at]), idx, spec.level),
            replanned_mix=dict(sorted(mix.items())),
            replanned_cost_inr=_interval(
                np.array([r.replanned.cost_inr for r in at]), idx, spec.level),
        ))
    return points


# -------------------------------------------------------------------- build


def _snapshot_reference(snapshot_dir: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
    payload = json.loads((snapshot_dir / "snapshot.json").read_text(encoding="utf-8"))
    return manifest["snapshot_id"], {
        s["scenario_id"]: s["counterfactual"] for s in payload["scenarios"]
    }


def check_against_snapshot(
    scenarios: Sequence[ScenarioSpeed], reference: dict[str, dict[str, Any]]
) -> list[str]:
    """Every difference between the zero-delay frozen arm and the snapshot."""
    problems = []
    for s in scenarios:
        if not s.in_snapshot or s.delays[0].delay_hours != 0:
            continue
        cf, at0 = reference[s.scenario_id], s.delays[0].frozen
        pairs = {
            "baseline_disruption": (s.unplanned.total, cf["baseline_disruption"]),
            "with_intervention_disruption": (at0.disruption_index,
                                             cf["with_intervention_disruption"]),
            "disruption_reduction_pct": (at0.disruption_cut_pct, cf["disruption_reduction_pct"]),
            "commerce_preserved": (at0.payments_kept_on_time_inr, cf["commerce_preserved"]),
            "merchants_protected": (at0.merchants_protected, cf["merchants_protected"]),
            "cost": (at0.cost_inr, cf["cost"]),
        }
        problems += [f"{s.scenario_id} {k}: {a!r} != {b!r}" for k, (a, b) in pairs.items()
                     if a != b]
    return problems


def build_speed(
    *,
    snapshot_dir: Path,
    seeds: Sequence[int] = SEEDS,
    scale: str = "small",
    delays: Sequence[float] = DELAYS_HOURS,
    families: tuple[ScenarioFamily, ...] | None = None,
    profile: ResourceProfile = ResourceProfile.SMALL_FAST,
) -> dict[str, Any]:
    """Replay every scenario on the delay grid. Returns the artifact payload."""
    if not delays or delays[0] != 0.0 or list(delays) != sorted(set(delays)):
        raise ValidationError("delays must be strictly increasing and start at 0",
                              delays=list(delays))
    snapshot_id, reference = _snapshot_reference(snapshot_dir)
    budget = budget_for(profile)
    scenarios: list[ScenarioSpeed] = []
    replay_configs: dict[str, Any] = {}
    dataset_versions: list[str] = []
    for seed in seeds:
        generator = scale_config(BenchmarkScale(scale), seed=seed)
        network = generate_network(generator)
        sim = SimulationConfig(horizon_hours=generator.horizon_hours, seed=seed)
        replay_configs[str(seed)] = sim.to_dict()
        dataset_versions.append(network.dataset_version)
        already = baseline_affected_set(network.graph, sim)
        for scenario in scenario_suite(
            network.graph, dataset_id=network.dataset_version, seed=seed,
            magnitude=MAGNITUDE, families=families, config=sim, baseline_affected=already,
        ):
            logger.info("speed_scenario", scenario_id=scenario.scenario_id,
                        family=str(scenario.spec.family), seed=seed)
            scenarios.append(scenario_speed(
                scenario, network=network.graph, seed=seed, sim=sim, budget=budget, delays=delays,
                in_snapshot=scenario.scenario_id in reference,
            ))

    problems = check_against_snapshot(scenarios, reference)
    if problems:
        raise ValidationError("the zero-delay plan does not reproduce the snapshot",
                              problems=problems)

    objective = ObjectiveSettings()
    provenance = SpeedProvenance(
        kind="replayed_offline",
        label=LABEL,
        delay_note=DELAY_NOTE,
        replay_note=REPLAY_NOTE,
        snapshot_id=snapshot_id,
        seeds=list(seeds),
        scale=scale,
        dataset_versions=dataset_versions,
        delays_hours=list(delays),
        optimiser="greedy_objective",
        objective={
            "gamma_delay": objective.gamma_delay,
            "gamma_default": objective.gamma_default,
            "gamma_deficit": objective.gamma_deficit,
            "delay_unit_hours": objective.delay_unit_hours,
        },
        replay_config_hash=config_hash(replay_configs),
        budget=budget.to_dict(),
        bootstrap=BOOTSTRAP,
        code_version=__version__,
    )
    return {
        "format": FORMAT,
        "provenance": provenance.model_dump(mode="json"),
        "points": [p.model_dump(mode="json") for p in aggregate(scenarios, delays)],
        "monotone_scenarios": sum(
            1 for s in scenarios if s.has_plan and s.monotonicity.frozen_non_increasing
            and s.monotonicity.replanned_non_increasing
        ),
        "scenarios": [s.model_dump(mode="json") for s in scenarios],
    }


def save_artifact(payload: dict[str, Any], root: Path, prefix: str, filename: str) -> Path:
    """Write an artifact under ``<prefix>-<hash>``; the bytes depend only on the payload."""
    digest = content_hash(payload)
    artifact_id = f"{prefix}-{digest[:16]}"
    directory = Path(root) / artifact_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    manifest = {
        "artifact_id": artifact_id,
        "content_hash": digest,
        "format": payload["format"],
        "snapshot_id": payload["provenance"]["snapshot_id"],
        "label": LABEL,
    }
    if "scenarios" in payload:
        manifest["n_scenarios"] = len(payload["scenarios"])
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return directory


def save_speed(payload: dict[str, Any], root: Path) -> Path:
    return save_artifact(payload, root, "speed", "speed.json")
