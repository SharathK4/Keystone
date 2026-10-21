"""Step-0 probe: can the repo's propagation be linearised into a branching matrix N?

Read-only. Loads the canonical snapshot's serving graph (estimated overlay) and
measures two candidate linearisations against the predictors they come from.
"""

import numpy as np

from lce.domain.shock import Shock, ShockComponent
from lce.models.propagation import (
    HawkesCascadePredictor,
    LinearThresholdPropagator,
    PropagationConfig,
)
from lce.snapshot.store import get_store

store = get_store()
g = store.graph
ids = sorted(g.merchant_ids)
ix = {m: i for i, m in enumerate(ids)}
n = len(ids)
H = 168.0

edges = [e for m in ids for e in g.out_dependencies(m)]
print(f"snapshot {store.snapshot_id}: {n} merchants, {len(edges)} estimated edges")
print("ground-truth edges in serving overlay:", sum(1 for e in edges if e.is_ground_truth))

# ---- L1: Hawkes cascade predictor, rare-event limit (noisy-OR -> sum) -------
N = np.zeros((n, n))
for e in edges:
    N[ix[e.source_id], ix[e.target_id]] = e.conditional_probability * e.lag.cdf(H)
rhoN = max(abs(np.linalg.eigvals(N)))
print(f"\nL1  N_ij = c_ij * F_ij(168h)     rho = {rhoN:.6f}")
print(f"    nnz={int((N > 0).sum())}  max entry={N.max():.4f}  mean nz={N[N > 0].mean():.4f}")
print(f"    max row sum={N.sum(1).max():.4f}  max col sum={N.sum(0).max():.4f}")
k = 1
P = N.copy()
while P.any() and k < 60:
    P = P @ N
    k += 1
print(f"    nilpotent: {not P.any()}  (N^{k} {'== 0' if not P.any() else '!= 0'})")

# ---- L2: linear-threshold propagator, saturated-buffer Jacobian -------------
M = np.zeros((n, n))
for m in ids:
    out = g.out_dependencies(m)
    tot = sum(e.pass_through for e in out)
    if tot <= 0:
        continue
    for e in out:
        M[ix[m], ix[e.target_id]] = (e.pass_through / tot) * (2.0 - e.reliability)
rhoM = max(abs(np.linalg.eigvals(M)))
print(f"\nL2  M_ij = share_ij * (2-rel_ij)  rho = {rhoM:.6f}   (buffers ignored: upper bound)")

# ---- E2-style consistency: one hop of the Hawkes predictor vs row of N ------
hp = HawkesCascadePredictor(PropagationConfig(max_hops=1))
lt = LinearThresholdPropagator()
errs = []
print("\nscenario                     origins  hop1 max|p - N row|   LT reached")
for sc in store.scenarios():
    origins = sc.shock.origin_merchants
    shock = Shock(
        name=sc.scenario_id,
        components=[
            ShockComponent(merchant_id=o, magnitude=sc.shock.magnitude, t=0.0)
            for o in origins
        ],
    )
    pred = hp.predict(g, shock)
    p = np.array([pred.exposures[m].exposure_score for m in ids])
    lin = np.zeros(n)
    for o in origins:
        lin += N[ix[o]]
    for o in origins:
        lin[ix[o]] = 1.0
        p[ix[o]] = 1.0
    err = float(np.abs(p - np.minimum(lin, 1.0)).max())
    errs.append((len(origins), err))
    ltp = lt.predict(g, shock)
    reached = sum(1 for m in ids if ltp.exposures[m].exposure_score >= 0.5 and m not in origins)
    print(f"{sc.family:28s} {len(origins):3d}     {err:.2e}            {reached}")

single = [e for k_, e in errs if k_ == 1]
print(f"\nsingle-origin max err: {max(single) if single else float('nan'):.2e}")
print(f"multi-origin  max err: {max([e for k_, e in errs if k_ > 1] or [float('nan')]):.2e}")
