import sys, time, json
sys.path.insert(0, "src")
from lce.data.generator import GeneratorConfig, generate_network
from lce.simulation import LiquiditySimulator, SimulationConfig, CounterfactualEvaluator, unit_shock
from lce.models.dependency import learn_dependencies
from lce.models.propagation import LinearThresholdPropagator, PropagationConfig
from lce.optimization import (generate_candidates, CandidateConfig, SearchConfig,
                              GreedySearch, CpSatSearch, ExhaustiveSearch, TopExposureSearch)

net = generate_network(GeneratorConfig(n_merchants=60, seed=3))
g = net.graph
g.clear_dependencies(); g.set_dependencies(learn_dependencies(g, t_end=0.0))
cfg = SimulationConfig(horizon_hours=168, seed=3)

anchor = net.anchors()[0]
shock = unit_shock(g, anchor, fraction_of_buffer=2.5)
pred = LinearThresholdPropagator(PropagationConfig(horizon_hours=168.0)).predict(g, shock)

cand = generate_candidates(g, shock, pred, CandidateConfig(top_k_nodes=4, max_candidates=20), horizon_hours=168.0)
print(f"candidates: {len(cand)} over nodes {cand.targeted_nodes}")

scfg = SearchConfig(budget=None, max_actions=2)
results = {}
for name, S in (("top_exposure", TopExposureSearch()), ("greedy", GreedySearch()),
                ("cp_sat", CpSatSearch()), ("exhaustive", ExhaustiveSearch())):
    ev = CounterfactualEvaluator(graph=g, shock=shock, config=cfg)
    t0=time.time()
    r = S.run(ev, cand.interventions, scfg)
    results[name] = r
    print(f"{name:13} actions={len(r.plan.interventions)} cost={r.cost:.3e} "
          f"prevented={r.disruption_prevented:.4e} DPR={r.disruption_prevented_per_rupee:.4f} "
          f"sims={r.simulations_run} {(time.time()-t0):.1f}s")

opt = results["exhaustive"].achieved_disruption
base = results["exhaustive"].baseline_disruption
print(f"\nbaseline D={base:.4e}  optimal D={opt:.4e}")
for name, r in results.items():
    achievable = base - opt
    gap = max(0.0, (r.achieved_disruption - opt) / achievable) if achievable > 1e-9 else 0.0
    print(f"  {name:13} optimality_gap={gap:.4f}")
