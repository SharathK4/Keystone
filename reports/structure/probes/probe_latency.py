"""Step-0 probe: does the selected plan's measured value decay with execution latency?

Read-only. Rebuilds the canonical scenarios exactly as `build_snapshot` does,
re-derives each scenario's selected plan with `run_scenario` (same config), then
replays that plan in the true simulator with every action landing `delta` hours
later than the plan scheduled it. Common Random Numbers make each difference a
causal effect of the delay alone.
"""

import time

from lce.benchmark.scales import BenchmarkScale, scale_config
from lce.benchmark.scenarios import baseline_affected_set, scenario_suite
from lce.data.generator import generate_network
from lce.intervention.evaluate import replay
from lce.intervention.experiment import Phase4Config, run_scenario
from lce.intervention.profiles import ResourceProfile
from lce.optimization.systemic import compute_systemic_importance
from lce.simulation.engine import SimulationConfig

DELTAS = (0.0, 1.0, 6.0, 12.0, 24.0, 48.0, 72.0)

gen = scale_config(BenchmarkScale("small"), seed=2025)
net = generate_network(gen)
graph = net.graph
H = gen.horizon_hours
sim = SimulationConfig(horizon_hours=H, seed=2025)
cfg = Phase4Config(profile=ResourceProfile.SMALL_FAST, seeds=(2025,), magnitude=2.0,
                   robust=True, systemic=True, pruning_benchmark=False)
sample = sorted(graph.merchant_ids)[: cfg.budget.systemic_sample or len(graph)]
systemic = compute_systemic_importance(graph, config=sim, merchants=sample)
already = baseline_affected_set(graph, sim)

for sc in scenario_suite(graph, dataset_id=net.dataset_version, seed=2025, magnitude=2.0,
                         config=sim, baseline_affected=already):
    t0 = time.perf_counter()
    res = run_scenario(sc, config=cfg, sim_config=sim, systemic=systemic, measure_pruning=False)
    out = res.counterfactual.by_name("model_guided_greedy")
    chosen = list(out.interventions) if out else []
    fam = str(sc.spec.family)
    if not chosen:
        print(f"\n{fam:22s} no plan ({time.perf_counter() - t0:.0f}s)")
        continue
    shocked = replay(graph, sc.shock, [], config=sim, run_id="probe:shocked")
    print(f"\n{fam:22s} plan: " + ", ".join(
        f"{u.type}@{u.merchant_id} t={u.t:.1f}h amt={u.amount / 1e5:.1f}L" for u in chosen)
        + f"   ({time.perf_counter() - t0:.0f}s)")
    print("   delta   commerce_kept   protected   disruption_cut%")
    for d in DELTAS:
        moved = [u.model_copy(update={"t": min(u.t + d, H - 1e-6)}) for u in chosen]
        tr = replay(graph, sc.shock, moved, config=sim, run_id=f"probe:d{d:g}")
        kept = shocked.value_delayed - tr.value_delayed
        prot = max(0, shocked.n_affected - tr.n_affected)
        cut = 100.0 * (shocked.disruption - tr.disruption) / shocked.disruption
        print(f"   {d:5.0f}h   {kept / 1e7:10.2f} Cr   {prot:6d}      {cut:7.2f}")
