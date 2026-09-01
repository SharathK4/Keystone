import sys, time, json
sys.path.insert(0, "src")
from lce.data.generator import GeneratorConfig, generate_network
from lce.models.dependency import DependencyLearner, compare_to_ground_truth

net = generate_network(GeneratorConfig(n_merchants=50, seed=11))
t0 = time.time()
edges = DependencyLearner().fit_graph(net.graph, t_end=0.0)
print(f"fit {len(edges)} edges in {time.time()-t0:.2f}s")
m = compare_to_ground_truth(edges, net.ground_truth_edges)
print(json.dumps({k: round(v,4) for k,v in m.items()}, indent=2))

# show a few side by side
truth = net.ground_truth_edges
shown = 0
for e in edges:
    if e.key in truth and shown < 6:
        t = truth[e.key]
        print(f"{e.source_id}->{e.target_id}: theta est={e.pass_through:.3f} true={t.pass_through:.3f} | "
              f"q est={e.conditional_probability:.3f} true={t.conditional_probability:.3f} | "
              f"lag est={e.lag.mean_hours:.1f} true={t.lag.mean_hours:.1f} | n={e.features.n_events}")
        shown += 1
