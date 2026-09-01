import sys, json, time
sys.path.insert(0, "src")
import numpy as np
from lce.data.generator import GeneratorConfig, generate_network
from lce.simulation import LiquiditySimulator, SimulationConfig, unit_shock
from lce.models.dependency import learn_dependencies
from lce.models.propagation import LinearThresholdPropagator, HawkesCascadePredictor, PropagationConfig
from lce.evaluation.metrics import classification_metrics, timing_metrics, attributable_affected

net = generate_network(GeneratorConfig(n_merchants=70, seed=5))
g = net.graph
cfg = SimulationConfig(horizon_hours=168, seed=5)
base = LiquiditySimulator(g, cfg).run(None, run_id="b")

# Use LEARNED edges (not ground truth) - this is the honest setting.
learned = learn_dependencies(g, t_end=0.0)
g.clear_dependencies(); g.set_dependencies(learned)

pcfg = PropagationConfig(horizon_hours=168.0)
lt, hk = LinearThresholdPropagator(pcfg), HawkesCascadePredictor(pcfg)

rows=[]
for anchor in net.anchors()[:5]:
    sh = unit_shock(g, anchor, fraction_of_buffer=2.0)
    truth_run = LiquiditySimulator(g, cfg).run(sh, run_id="s")
    truth = attributable_affected(truth_run.affected_ids, base.affected_ids)
    if not truth:
        print(f"{anchor}: no attributable contagion, skipping"); continue
    for name, model in (("linear_threshold", lt), ("hawkes", hk)):
        p = model.predict(g, sh)
        cm = classification_metrics(p.scores(), truth, universe=g.merchant_ids, threshold=0.5)
        tm = timing_metrics(p.hit_times(), truth_run.hit_times(), restrict_to=truth)
        rows.append((anchor, name, len(truth), cm.precision, cm.recall, cm.f1, cm.pr_auc, tm.mae_hours, tm.n_compared))

print(f"{'anchor':8} {'model':17} {'|A|':>4} {'prec':>6} {'rec':>6} {'f1':>6} {'prauc':>7} {'mae_h':>7} {'n_t':>4}")
for r in rows:
    pr = f"{r[6]:.3f}" if r[6] is not None else "  n/a"
    mae = f"{r[7]:.1f}" if r[7] is not None else " n/a"
    print(f"{r[0]:8} {r[1]:17} {r[2]:4d} {r[3]:6.3f} {r[4]:6.3f} {r[5]:6.3f} {pr:>7} {mae:>7} {r[8]:4d}")
