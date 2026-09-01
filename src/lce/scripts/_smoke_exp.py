import sys, json, time
sys.path.insert(0, "src")
from lce.experiments import ExperimentConfig, ExperimentRunner, quick_config

cfg = quick_config(n_merchants=45, seed=17, n_shocks=3, name="smoke")
print("config_hash:", cfg.config_hash, "dataset:", cfg.dataset_version)
t0=time.time()
rep = ExperimentRunner(cfg).run()
print(f"elapsed {time.time()-t0:.1f}s")
d = rep.to_dict()
print(json.dumps({k:v for k,v in d.items() if k not in ("network","seeds")}, indent=2, default=str))

# reproducibility: same config -> same hash and same dependency metrics
cfg2 = quick_config(n_merchants=45, seed=17, n_shocks=3, name="smoke")
rep2 = ExperimentRunner(cfg2).run()
same = (cfg.config_hash == cfg2.config_hash and
        abs(rep.dependency_metrics.get("pass_through_mae",0) - rep2.dependency_metrics.get("pass_through_mae",0)) < 1e-12)
print("REPRODUCIBLE:", same)
