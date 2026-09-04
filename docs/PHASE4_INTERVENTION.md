# Phase 4 - counterfactual intervention and production inference

Phases 1-3 are frozen. This document defines the intervention problem, the exact
and scalable solvers, the counterfactual evaluation protocol, systemic
importance, uncertainty handling, the laptop budget, and what the inference
package guarantees. It also states plainly what these results can and cannot
support.

---

## 1. Implementation map - what is reused

Phase 4 adds an optimisation and serving layer on top of machinery that already
exists and is already validated. Nothing below is reimplemented.

| existing module | what Phase 4 uses it for | changed? |
|---|---|---|
| `simulation.engine.LiquiditySimulator` | the *only* evaluator of `D`. Every intervention value is a real simulation | no |
| `simulation.counterfactual.CounterfactualEvaluator` | cached `D(G,S,U)` with common random numbers across plans | no |
| `domain.objectives.compute_disruption` | `D` itself: delay + default + deficit terms | no |
| `domain.intervention.Intervention` / `InterventionPlan` | the five typed action objects and their cost model | **one additive field** (`provenance`) |
| `domain.enums.InterventionType` | the action taxonomy | no |
| `simulation.engine._apply_intervention` | the simulation semantics of each action | no |
| `optimization.candidates.generate_candidates` | Stage-1 seed set, wrapped by the Phase-4 scorer | no |
| `optimization.search.{Greedy,CpSat,Exhaustive,TopExposure}Search` | Stage-2 search strategies | no |
| `optimization.systemic.compute_systemic_importance` | simulated `SI_i` sweep | **extended additively** |
| `benchmark.scales` / `benchmark.scenarios` | networks and shocks; unchanged Phase-2 definitions | no |
| `benchmark.ground_truth` | scenario truth for the prediction half | no |
| `learning.*` (Phase 3) | contagion prediction, calibration, temporal splits, model artifacts | no |
| `models.registry.ModelRegistry` | artifact storage and manifests for the inference bundle | no |
| `razorpay.client.RazorpayClient` | Test-Mode Orders/Payments for the execution provider | no |
| `services.import_service.RazorpayImporter` | idempotent Test-Mode import | no |
| `api.app` / `api.deps` | app factory, error mapping, request ids | **one router registered** |
| `seeds`, `experiments.tracker` | determinism and provenance | no |

New (`src/lce/intervention/`): `problem`, `actions`, `exact`, `scalable`,
`robust`, `evaluate`, `profiles`, `experiment`, `cli`. The strategy comparison of
section M lives in `experiment.run_phase4` and `Phase4Report.summary`, not in a
separate module - it is the same sweep that produces the run artifact, and
splitting it would mean two definitions of what a strategy is.

New (`src/lce/inference/`): `artifact`, `predictor`, `service`, `schemas`,
`export`, `cli`.
New (`src/lce/execution/`): `providers`.
New API router: `api/routers/inference.py`.
New scripts: `src/lce/scripts/verify_phase4.py`.

Two additive changes to Phase-3 code, both mechanical and behaviour-preserving:
`lce/learning/__init__.py` resolves its exports lazily, and the design-matrix
layout moved into `learning/features.build_hazard_design`. Both exist for the
same reason - the serving package must be able to build a feature table without
importing the dataset generator - and a test asserts it stays that way.

## 2. The intervention problem

### 2.1 Given

| symbol | meaning |
|---|---|
| `G_t` | observable temporal payment network at the decision time `t` |
| `L_t` | merchant liquidity state: `L_i(t)`, `K_i(t)`, floor `Lfloor_i` |
| `S` | the shock, onset `t0` |
| `H` | prediction/decision horizon (simulator horizon `T`) |
| `A` | the feasible action set |

An action `a` is a set of at most `k` typed interventions. Evaluating it means
running the true simulator:

```
(G_t, L_t, S, a)  ->  trajectory  ->  D(a)
```

`D` is the Phase-1 disruption objective, unchanged:

```
D = sum_i w_i [ gamma_1 * sum_o a_o phi(delta_o)   value-weighted delay, INR-days
              + gamma_2 * defaults_i                default count
              + gamma_3 * integral (Lfloor_i - L_i(t))^+ dt   deficit, INR-hours ]
```

Every term is an existing financial quantity produced by the simulator. No new
score is invented.

### 2.2 Primary (penalised) form

```
minimise   J(a) = D(a) + lambda * Cost(a)
   a in A
```

`Cost(a)` is the sum of the per-action costs already defined in
`domain.intervention`: capital at risk for injections, expected draw for credit
lines, factoring fee for acceleration, carry fee for extension, admin fee for
restructuring. `lambda` converts rupees of capital into units of `D`; it is a
declared preference parameter, recorded in every run, never fitted.

### 2.3 Constrained (epsilon) form

```
minimise   Cost(a)      subject to   D(a) <= epsilon
   a in A
```

Both forms are solved over the same feasible set and the same simulated `D`, so
they are directly comparable. `epsilon` is configurable; the default is a
fraction of the do-nothing disruption.

### 2.4 Feasible set `A`

An action is feasible only if **all** of the following hold. Each is checked
explicitly and violations are reported rather than silently dropped.

| constraint | statement |
|---|---|
| budget | `Cost(a) <= B` |
| cardinality | `\|a\| <= k` |
| capacity | at most `c_i` interventions per merchant (default 1) |
| timing | `t_u` inside `[0, T)`, and not before the decision time |
| max term extension | `shift_hours <= max_extension_hours` for a `SUPPLIER_TERM_EXTENSION` |
| max acceleration | a receivable cannot be pulled earlier than the decision time |
| max repayment modification | tranches `<= max_tranches`, and total span `<= max_restructure_span_hours` |
| liquidity floor | an action must not make any merchant's floor breach *worse than it would have been with no action*, checked on the realised trajectory |
| deadline | an obligation's deadline may move only within the stated bound, and a restructure's last tranche must fall inside the horizon |
| no money creation | conservation: the total obligated principal is unchanged by any action except an injection, whose cost equals the cash it adds |

The liquidity-floor constraint is checked **during the search**, not only at
evaluation. It cannot be tested structurally - whether an action pushes a
merchant deeper below its floor is a property of the realised trajectory - so a
solver that only screens action parameters will happily *choose* a plan it is not
allowed to take and discover the violation afterwards. The greedy and exact
solvers therefore evaluate it on each candidate set they simulate (free, since
the run is already cached) and skip violating sets. The MILP cannot: its
surrogate knows disruption totals and not trajectories, so it re-simulates its
choice and *reports* a violation rather than hiding one.

The constraint is **incremental**, and it has to be. On a
shocked network a great many merchants dip below their operating floor with no
intervention at all - that is exactly what the deficit term of the objective
prices - so an absolute check flags every one of them and attributes the shock's
damage to whoever acted. The constraint that actually constrains an action is
that it must not make things worse than doing nothing, and a term extension can
genuinely violate it by moving a payable into a week where the debtor has less
cash.

**No artificial money creation** is the invariant that matters most, and it is
tested directly: for every action type other than `LIQUIDITY_INJECTION` and
`CREDIT_LINE_INCREASE`, the sum of obligation principal before and after the
action is equal to within tolerance. Restructuring changes *when* cash is owed,
never *how much* - the simulator's `_restructure` already enforces this by
construction, and the test pins it.

## 3. Solvers

### 3.1 Exact - the scientific ground truth

On small networks the exact optimum is obtained by **complete enumeration over
feasible subsets, each evaluated with the true simulator**, then taking the
argmin of `J`. That is exact because `D` is a black box: there is no algebraic
structure to exploit, so completeness is the only proof available.

The MILP is not a shortcut around that, and is not presented as one. CP-SAT is
used for the *selection* layer over enumerated columns, where it contributes
what enumeration alone does not: a solver status, a bound, and a natural
encoding of the `epsilon`-constrained form. Its runtime and gap are reported.

**What the reference is optimal over.** The exact optimum is computed on the
union of every strategy's actions - the model's candidate set *and* each naive
rule's choice - not on the model's candidates alone. A reference restricted to
what the model proposed is not a reference: a naive rule picking outside that set
beats it, and the resulting "optimality gap" comes back negative, which says the
reference was wrong rather than that the heuristic was brilliant. This was
observed and fixed during development; the acceptance harness now asserts the
reference dominates every reported strategy.

**Near-exact MILP.** For instances too large to enumerate but small enough to
afford `O(n^2)` simulations, a CP-SAT model is solved over a **simulated
pairwise surrogate**:

```
D_hat(a) = D(empty) - sum_i g_i x_i + sum_{i<j} r_ij x_i x_j
```

where `g_i` is the measured singleton gain and `r_ij` the measured interaction
residual `g_i + g_j - g_ij`. Products are linearised with the standard
`y_ij <= x_i, y_ij <= x_j, y_ij >= x_i + x_j - 1`. This is exact for the
surrogate and strictly better than the Phase-1 linear one, which assumed
`r_ij = 0`. The chosen set is always **re-simulated** before it is reported;
the surrogate never appears in a result.

### 3.2 Scalable - two stages

Exhaustive enumeration is prohibited above `SMALL`.

**Stage 1 - candidate generation.** Proposals come from two sources, because one
is demonstrably not enough:

* the Phase-1 generator, which sizes each action type against a node's predicted
  shortfall - but only proposes for nodes with a strictly positive exposure
  score;
* a **coverage-gap** rule, which proposes an injection of
  `horizon payables + shock - buffer` for the merchants with the largest value at
  risk, regardless of what any model thinks of them.

The second rule exists because of a measured failure. The objective is
value-weighted, so a large, well-capitalised merchant slipping contributes far
more disruption than a fragile micro merchant failing outright - and its modelled
failure probability can still be near zero. Those nodes were never entering the
pool, so the pruning benchmark reported the optimum being lost. Adding them on a
measurable criterion fixed the generation half of that.

Every candidate then carries an explainable score, a documented combination of
measurable factors rather than a model output:

| factor | source |
|---|---|
| predicted marginal downstream disruption | Phase-3 calibrated `F_i(T)` times the node's downstream obligation value |
| earliest predicted constraint | Phase-3 `tau_hat_i` |
| propagation centrality | Katz centrality on the observed payment graph |
| intervention leverage | live obligation value at the node over its buffer |
| cost | the action's own cost |

Candidates are **filtered by feasibility first** and only then ranked. A model
score can promote a candidate; it can never make an infeasible one eligible.

The retained set is drawn **alternately from two rankings**: the weighted score
above, and an upper bound on net benefit in rupees (`protected value - lambda *
cost`). A single weighted sum cannot serve both - ranking by the model score
alone cuts the high-value actions, and re-weighting to keep them turns the score
into the second ranking anyway. Neither dominates, both are explainable, and the
pruning benchmark measures what the merge costs.

Retention is also **capped per merchant**, at exactly the capacity limit. The
feasible set allows at most `max_per_merchant` actions on any one merchant, so a
slot spent on a second action for a merchant already represented can only
substitute for the first - it cannot put a plan in reach that was not already
there. Merchant coverage is what widens the reachable plan space.

This was found by the pruning benchmark rather than by inspection, and the cap
value was measured rather than argued. Uncapped, twelve retained candidates
covered five merchants (seven slots were alternatives on one node) and the pool
optimum was lost with a relative regret of 0.44 to 1.69. A cap of two - kept
initially so the search would have a choice of action *type* - covered seven
merchants and still lost it. A cap of one covers eleven or twelve, recovers the
optimum exactly on one probe network and halves the regret on the other. The
"choice of type" argument was wrong, and the benchmark is what said so.

**Residual limitation.** Even at a cap of one, retention still loses the optimum
on one of the two probe networks (relative regret 0.98). Twelve candidates out of
forty-five is a 27% filter, and no ranking recovers every optimum at that rate.
The number is reported per run rather than smoothed away; raising
`max_candidates` is the direct lever, at a quadratic cost in the exact solve.

**Stage 2 - search.** A deterministic search over the reduced set, evaluated by
the true simulator: greedy on the stated objective, or the pairwise MILP.

**Pruning is benchmarked**, not asserted: candidates before and after, whether
the exact optimum survived, the **relative** regret when it did not, and the
runtime reduction. Both the boolean and the relative number are reported, because
the boolean is exact to a rounding tolerance and cannot distinguish a filter that
loses half a percent of the objective from one that loses half of it.

## 4. Counterfactual evaluation

The protocol, in order, and the order is the point:

1. Predict the cascade from **observable pre-shock information only** (Phase-3
   barrier, unchanged).
2. Select an intervention using the model-based procedure.
3. **Replay that selection in the true simulator.**
4. Compare against: no intervention, naive largest-deficit, highest-degree,
   highest systemic importance, cash-cover, and the exact optimum where
   tractable.

Reported per instance: predicted disruption, true disruption, cost, commerce
preserved (value delayed that no longer is), disruption reduction %, capital
efficiency (`disruption prevented / cost`), absolute and relative optimality
gap, regret against the exact optimum, feasibility violations, and runtime.

A model-selected intervention is **never** scored on the model's own predicted
outcome. The number that is reported is always the replayed one.

## 5. Systemic importance

For merchant `i`, with `S_i` a shock standardised to a fixed fraction of that
merchant's own liquidity slack:

```
SI_i = D(G, S_i) - D(G, no shock)
```

Normalised two ways: against the worst node (readability), and against the
merchant's own scale (`SI_i / throughput_i`), which is what separates
"structurally load-bearing" from "large". Reported alongside: downstream
affected count, downstream delayed value, cascade depth, and time-to-impact.

Ranking by GMV, degree, or cash deficit is explicitly *not* the measure; those
are computed as **baselines** and the rank correlation against `SI` is reported,
so the claim that `SI` is not simply a size proxy is measured rather than
asserted.

## 6. Uncertainty and robustness

The optimiser must not spend capital on one point prediction.

Phase 3 supplies calibrated `F_i(t)`. Phase 4 adds deterministic **scenario
perturbations**: a small, fixed, seed-derived family of plausible worlds
(shock magnitude scaled, onset shifted, settlement lag varied). An action is
evaluated in every world.

```
J_robust(a) = mean_s D_s(a) + kappa * spread_s D_s(a) + lambda * Cost(a)
```

with `spread` the standard deviation across worlds (configurable to CVaR).
`kappa = 0` recovers the expected-value objective exactly, so the robust mode is
a superset, not a different system. Perturbations are deterministic given the
seed, so a robust decision is as reproducible as a nominal one.

Full posterior propagation is out of scope on a laptop and is not claimed.

## 7. Complexity and laptop budget

| stage | cost | note |
|---|---|---|
| candidate generation | `O(n log n)` | no simulation |
| singleton gains | `n` simulations | Stage-2 entry cost |
| pairwise surrogate | `n(n-1)/2` simulations | only under `MEDIUM` and below |
| greedy | `k * n` simulations | the default |
| exhaustive | `sum_{j<=k} C(n,j)` simulations | `SMALL` only, hard-capped |
| systemic sweep | one simulation per merchant | restrict with `merchants=` |

Three profiles bound every one of these:

| profile | network | candidates | max actions | solver time | exact optimum | target runtime |
|---|---|---|---|---|---|---|
| `SMALL_FAST` | 100 merchants | 12 | 2 | 5s | yes | under a minute |
| `MEDIUM` | 1,000 | 24 | 3 | 15s | no | a few minutes |
| `LARGE_DEMO` | 10,000 | 32 | 3 | 30s | no | bounded, greedy only |

CPU-first throughout. Torch is optional and only the graph predictor uses it.
No cluster, no cloud, no GPU requirement. Where a component cannot be made
rigorous inside the budget, the smaller mathematically correct version is
implemented and the scaling boundary is stated here rather than faked.

## 8. Reproducibility

Every run writes `reports/phase4/<run_id>/result.json` containing `prediction`,
`intervention`, `counterfactual`, `evaluation`, `timing` and `provenance`. The
provenance block records dataset id and version, seed, model version and
artifact hash, feature schema version, simulator config, optimizer and solver
configuration, intervention configuration, code version, and host/runtime
metadata.

Given the same inputs and seed the decision and its evaluation reproduce
exactly, with one documented exception: CP-SAT is run single-threaded for
determinism, but a solver that hits its time limit may return a different
feasible solution on a differently-loaded machine. That case is flagged in the
result by its status field.

## 9. Inference packaging

Training and inference are separate. The exported bundle contains model weights,
architecture config, the feature schema, normalisation and calibration
parameters, the model version, and training dataset metadata, plus a content
hash over the whole bundle.

The inference package loads that bundle and nothing else - no generator, no
optimiser training code, no notebooks. The service loads once at startup.
Endpoints are versioned under `/api/v1`. An integration test proves the full
path: train, save, load in a process that imports only the inference package,
serve a request, get a valid prediction.

## 10. Razorpay

No live financial movement in this phase. Execution is provider-agnostic:
`SimulationProvider` applies an action to the simulator; `RazorpayTestProvider`
maps it onto Test-Mode operations using only documented APIs, reading
credentials from the environment. Direct Transfers are never assumed available -
capability is probed and the provider degrades to a recorded, unexecuted plan if
absent. Secrets never enter logs.

## 11. Limitations - what these results do and do not support

**Supported.** That on this benchmark, a model-guided intervention selected from
observable pre-shock information and replayed in the true simulator reduces
measured disruption relative to no intervention and to the named naive
baselines, by the margins reported with their intervals; and that on `SMALL`
networks the gap to a complete-enumeration optimum is what is reported.

**Not supported.** Any claim about real merchant behaviour, real recovery rates,
or borrower psychology. The repayment-restructuring action changes the schedule
of obligations in a simulator; it makes no claim about how a real borrower would
respond. Nothing here has been validated against live payment data, and the
economic parameters (`lambda`, the cost rates, the objective gammas) are declared
assumptions, not estimates.

**Bounded.** `LARGE_DEMO` demonstrates that the pipeline runs at 10,000
merchants; it does not demonstrate optimality there, because no exact optimum is
computed at that scale.
