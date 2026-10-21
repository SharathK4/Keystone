"""Step-0 probe: what does the frozen hazard artifact predict on the canonical scenarios?

Read-only. Regenerates the canonical network (seed 2025, small) exactly as
`build_snapshot` does, rebuilds the scenario suite, and serves each scenario
through the production inference path (`InferenceService.predict_contagion`),
mirroring `intervention.experiment.predict_with_artifact`. Nothing is written.
"""

import numpy as np

from lce.benchmark.scales import BenchmarkScale, scale_config
from lce.benchmark.scenarios import baseline_affected_set, scenario_suite
from lce.data.generator import generate_network
from lce.inference.predictor import NetworkState
from lce.inference.service import InferenceService
from lce.learning.problem import baseline_payment_stream, build_observed_window
from lce.simulation.engine import SimulationConfig

gen = scale_config(BenchmarkScale("small"), seed=2025)
net = generate_network(gen)
graph = net.graph
sim = SimulationConfig(horizon_hours=gen.horizon_hours, seed=2025)
print("dataset", net.dataset_version, "merchants", len(graph),
      "payments", len(graph.payment_events))

svc = InferenceService()
print("artifact", svc.model_version, "threshold", svc.artifact.manifest.threshold)

already = baseline_affected_set(graph, sim)
for sc in scenario_suite(graph, dataset_id=net.dataset_version, seed=2025, magnitude=2.0,
                         config=sim, baseline_affected=already):
    stream = baseline_payment_stream(sc.unperturbed_graph, sim)
    win = build_observed_window(sc, config=sim, baseline_payments=stream)
    state = NetworkState(network_id=sc.dataset_id, merchants=list(win.graph.merchants.values()),
                         obligations=win.graph.obligations, payments=win.graph.payment_events)
    pred, ms = svc.predict_contagion(state, sc.shock, observation_cutoff=win.origin_t,
                                     horizon_hours=sim.horizon_hours)
    p = np.array([n.probability_constrained for n in pred.nodes])
    origins = {c.merchant_id for c in sc.shock.components}
    nonorig = np.array(
        [n.probability_constrained for n in pred.nodes if n.merchant_id not in origins]
    )
    top = sorted(pred.nodes, key=lambda n: -n.probability_constrained)[:5]
    print(f"\n{sc.spec.family!s:24s} cutoff={win.origin_t:6.1f}h  "
          f"origins={sorted(origins)}  {ms:.0f}ms")
    print(f"   intervals: {[round(e) for e in pred.interval_edges]}")
    print(f"   P>=0.05: {(p >= .05).sum():3d}   P>=0.2: {(p >= .2).sum():3d}   "
          f"P>=0.5: {(p >= .5).sum():3d}"
          f"   non-origin max P: {nonorig.max():.3f}   median P: {np.median(p):.4f}")
    for nd in top:
        tag = "origin" if nd.merchant_id in origins else ""
        ett = nd.expected_time_to_constraint_hours
        print(f"     {nd.merchant_id}  P={nd.probability_constrained:.3f}  "
              f"E[T|T<H]={ett:6.1f}h  {tag}")
