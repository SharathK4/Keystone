# Phase 3 - the statistical learning problem

Phase 2 is frozen. This document defines what Phase 3 *learns*, from what, against
what, and what it is forbidden to see. Nothing here changes the generator, the
simulator, the ground-truth definitions, the scenario families or the validation
invariants; every quantity below is read out of machinery that already exists.

---

## 1. Objects

A benchmark example is a pair `(d, s)`: dataset `d` (a generator seed) and
scenario `s` (a shock built by `lce.benchmark.scenarios.build_scenario`).

| symbol | meaning | where it lives |
|---|---|---|
| `G = (V, E)` | temporal payment multigraph, `n` merchants | `TemporalPaymentGraph` |
| `E = {(i -> j, a, t)}` | marked point process of realised payments | `graph.payment_events` |
| `O = {o = (i -> j, a_o, s_o, d_o)}` | obligation book: amount, issue time, deadline | `graph.obligations` |
| `Theta = {(theta_ij, q_ij, Lambda_ij, r_ij)}` | **latent** dependency overlay: pass-through, trigger probability, lag law, reliability | `SyntheticNetwork.ground_truth_edges` |
| `S` | shock, onset `t0 = min_c t_c` | `BuiltScenario.shock` |
| `b_i(t) = L_i(t) + K_i(t) - Lfloor_i` | liquidity buffer | `NodeState.buffer` |
| `C_i(t) in {0,1}` | node `i` is liquidity-constrained at `t` | `NodeOutcome.first_constrained_t` |

## 2. Prediction origin and the observable filtration

The **origin** is the shock onset, `t0 := S.onset_t`. This is the moment a real
deployment learns something is wrong ("this merchant's inflow has failed") and
must answer *who else, and when*.

The observable sigma-algebra is

```
F(t0) = sigma(  {e in E : t_e < t0}                    payments strictly before the origin
              , {o in O : s_o < t0} -> (a_o, d_o, kind, priority, paid-so-far)
              , {X_i}                                  static merchant disclosures
              , S_tilde )                              observable shock descriptor
```

with

* `X_i = (sector, tier, L_i(0), K_i, Lfloor_i)` - balance-sheet **disclosures**. A
  working-capital lender obtains these at onboarding. This is an explicit
  assumption, and ablation `no_balance_sheet` measures how much it is worth.
* `S_tilde = (origin merchants, onset time, magnitude, kind)` - the trigger itself,
  which is what the operator reports. Ablation `no_shock_descriptor` removes it.
* payments in `[0, t0)` come from the **no-shock baseline run**. Under common
  random numbers the shocked and unshocked worlds are identical before `t0`, so
  this is the real pre-origin stream in both, not a counterfactual.

### Forbidden - the leakage barrier

| forbidden | why |
|---|---|
| `Theta` in any form | it is the thing being estimated (Task 3) and the mechanism being predicted (Tasks 1-2) |
| any event with `t >= t0` | future |
| the **perturbed** obligation book | `DELAYED_INFLOW` / `SUPPLIER_FAILURE` mutate deadlines and statuses *at* the origin; features read `BuiltScenario.unperturbed_graph`, never `.graph` |
| `payment_discipline pi_i` | latent behavioural parameter that drives the simulator's discretionary lateness directly |
| `lambda_i` (exogenous inflow), `mu_i` (operating burn) | parameters of the *off-network* cash process. A payments platform does not see a merchant's payroll or rent, and `b_i(0)/(mu_i - lambda_i)` is very nearly the answer |
| `systemic_weight w_i` | an objective weight, not an input |
| anything in `ScenarioGroundTruth` | by construction |

Enforcement is structural and then checked. The barrier is
`lce.learning.problem.build_observed_window`, which scrubs the latent profile
fields, drops every event at or after the origin, clears the dependency overlay
and reads the book off the unperturbed graph - so a feature builder handed an
`ObservedWindow` cannot reach a forbidden quantity at all. `audit_window` then
opens a window and asserts directly that it holds none of them, and
`audit_leakage` guards against regressions in the barrier by perturbing each
forbidden input and requiring the feature matrix back bit-identical. The test
suite includes negative controls that disable the scrub and widen the cutoff, so
the audits are known to fire.

## 3. Targets

Let `A(s) = A(G,S) \ A(G,0)` be the shock-attributable affected set (Phase-2
definition, unchanged), and for `i in A(s)` let `tau_i = first_constraint_t(i) - t0`.
Nodes outside `A(s)` are **right-censored** at `T - t0`.

**Task 1 - probability of becoming liquidity-constrained by time `t`**

```
F_i(t) = P( tau_i <= t | F(t0) ),      t in {6, 24, 48, 72, T-t0} hours
```

**Task 2 - time-to-constraint / propagation timing**

```
tau_hat_i ~ E[ tau_i | tau_i <= T-t0, F(t0) ]
```

scored in hours, plus a concordance index over the predicted ordering.

Both come from one object if the model is written as a **discrete-time hazard**:
with intervals `I_1..I_K` partitioning `(t0, T]` and hazard
`h_ik = P(tau_i in I_k | tau_i >= start I_k, F(t0))`,

```
F_i(t)      = 1 - prod_{k : end(I_k) <= t} (1 - h_ik)
tau_hat_i   = sum_k mid(I_k) * h_ik * prod_{l<k} (1 - h_il)
```

and the likelihood is the standard censored survival log-likelihood

```
l = sum_i [ sum_{k < K_i} log(1 - h_ik) + 1{hit} * log h_{i,K_i} ]
```

**Task 3 - hidden pairwise dependency strength**

Estimate `theta_hat_ij, q_hat_ij, lag_hat_ij` from `{e in E : t_e < t0}` alone.
Scored against `Theta` with `lce.models.dependency.compare_to_ground_truth`.
**`Theta` is never a training input** in the main experiment: the estimator is the
unsupervised marked-Hawkes EM. A separately-named supervised regression onto
`Theta` exists only as a declared upper bound and never feeds Tasks 1-2.

## 4. Temporal train / validation / test split

Every example carries an absolute origin `T_abs(d,s) = epoch(d) + t0(s)`, where
`epoch(d) = rank(d) * (H + T)` stamps dataset `d` into a sequence. Within a
dataset the ordering is real time; across datasets it is a protocol convention -
stated plainly because it buys the guarantee that matters:

> **no example in train or val has a label window that reaches into, or past, the
> origin of any test example.**

Blocks are cut in `T_abs` order - train, then val, then test - with a **purge
band** between consecutive blocks so no training label window overlaps a test
feature window. Datasets are disjoint across blocks, so no merchant identity is
shared either. `verify_split` asserts both properties and the suite tests them.

## 5. Models, in implementation order

| # | name | family | what it adds |
|---|---|---|---|
| 0 | `PrevalenceBaseline` | trivial | the constant every other model must beat |
| 1 | `CashCoverBaseline` | heuristic | payables due by `t` over `b_i(0)` - no network at all |
| 2 | `ShockDistanceBaseline` | structural | hops from the shock on the *observed* payment graph |
| 3 | `DiscreteTimeHazard` | classical statistical | regularised discrete-time survival on leak-free tabular features; native `F_i(t)` and `tau_hat_i` |
| 4 | `HawkesContagionModel` | temporal point process | marked-Hawkes `Theta_hat` from pre-origin events, then `LinearThresholdPropagator` |
| 5 | `TemporalGraphModel` | temporal graph network | the Phase-1 GATv2 trunk over the *learned* overlay `Theta_hat`, leak-free features |

`SIMULATION_ORACLE` is the reference, not a competitor.

## 6. Calibration

Fitted on **validation only**, never test: isotonic (PAVA) and Platt. Reported
with Brier score, log-loss, ECE (equal-mass bins), MCE, calibration
slope/intercept and the reliability curve.

### Uncertainty

Downstream positives are well under one percent of the scored universe, so a
bare PR-AUC is not something to rank models by. Every headline figure carries a
**clustered bootstrap** interval that resamples *scenarios*, not nodes: the few
hundred node rows inside one scenario share a network, a shock and a baseline
run, and resampling them independently would treat one cascade as many
observations and report an interval several times too narrow.

## 7. Ablations

| name | question |
|---|---|
| `no_graph` | is the network worth anything over per-merchant features? |
| `true_structure` | how much of the gap is dependency-estimation error? (oracle `Theta`, upper bound) |
| `shuffled_edges` | negative control: same degrees, destroyed structure |
| `no_edges` | graph neural network, or just neural network? |
| `no_balance_sheet` | how much of the result rests on the disclosure assumption? |
| `no_shock_descriptor` | can the model find the origin itself? |
| `supervised_dependency` | Task-3 upper bound: ridge on pair features onto `theta` |

## 8. Reuse map - what Phase 3 does *not* rewrite

| existing module | used for |
|---|---|
| `benchmark.scales`, `benchmark.scenarios` | dataset + scenario construction (unchanged) |
| `benchmark.ground_truth` | labels; `observable_graph()`; `first_constraint_t`; `cascade_depth` |
| `simulation.engine` | baseline run for the pre-origin observable stream, and the labels |
| `models.hawkes`, `models.dependency` | Task 3 estimator, unchanged |
| `models.features` | edge descriptive statistics |
| `models.propagation` | mechanistic predictor behind model 4 |
| `models.tgnn` | GATv2 trunk, training loop, save/load for model 5 |
| `models.registry` | artifact + manifest storage |
| `evaluation.metrics` | PR-AUC, ROC-AUC, timing MAE, `attributable_affected` |
| `evaluation.harness` | `GroundTruth`, `evaluate_prediction` |
| `domain.prediction` | `ModelPrediction` / `NodeExposure` output contract |
| `seeds`, `experiments.tracker` | determinism and run provenance |

Newly written (`src/lce/learning/`): `problem`, `features`, `dataset`, `splits`,
`baselines`, `pointprocess`, `graphmodel`, `calibration`, `evaluation`,
`ablations`, `experiment`, `cli`. Plus `src/lce/scripts/run_phase3.py`,
`tests/test_learning.py` and `LeakageError` in `lce.errors`.

Two additive changes outside the new package, both backward compatible:
`TGNNConfig` gains `node_feature_dim` / `edge_feature_dim` (defaulting to today's
constants) and `TemporalGNNPredictor` gains `predict_from_sample`, so the Phase-3
feature builder can drive the existing trunk without duplicating it.

## 9. Reproducing

```
python src/lce/scripts/run_phase3.py --seeds 101 ... 124 --ablations --out reports/phase3.json
```

or through the CLI:

```
lce learn spec           # what is observable, what is latent, what is predicted
lce learn split          # build a corpus and verify the temporal split
lce learn run --ablations --out reports/phase3.json
lce learn dependency     # task 3 alone
```

A result is quoted with its `config_hash`, which covers the seed list, the scale,
the shock magnitude, the observation spec, the task discretisation, the split
fractions and the model set. Two runs with the same hash produce the same
numbers; a different number with the same hash is a bug, not a draw.

`--full-ablations` additionally runs `no_balance_sheet` and
`no_shock_descriptor`, which rebuild the corpus under a restricted observation
spec and roughly triple the runtime.
