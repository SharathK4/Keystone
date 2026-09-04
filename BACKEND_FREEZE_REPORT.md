# Backend Freeze Report

**Status:** FROZEN
**Version:** `lce` 0.1.0
**Date:** 2026-09-02
**Scope:** Phases 1–5 complete. Backend is an analytical/data service. No frontend in this repository.

---

## 1. What this system is, and what it is not

It estimates how a liquidity shock to one merchant propagates through a payment
network, predicts which merchants become liquidity-constrained and when,
recommends interventions that reduce the damage, and reports what each
recommendation actually achieved when replayed.

Three data regimes are kept separate on purpose, and the separation is visible
in every payload's provenance block:

| Regime | Role | Where it appears |
| --- | --- | --- |
| Synthetic generator | Controlled ground truth. Latent dependency strengths, pass-through and lags are known, so a model can be *scored* rather than merely demonstrated. | Every metric in §6 |
| Razorpay Test Mode | Real provider integration: connectivity, capability probing, ingestion and webhook provenance. No funds move. | §7 |
| Public aggregate data | External context and calibration of scale only. | Generator config |

**No claim is made about real-world predictive accuracy.** Every number in §6
is measured on synthetic networks whose generating process is known. That is
what makes them measurable; it is also what makes them not evidence about live
merchant behaviour.

Intervention outputs are **decision recommendations, not a lending product**.
An offer contract is a structured proposal with an indicative cost computed from
a declared assumption (`FACILITY_FEE_RATE_PER_DAY = 0.0004`, ≈14.6% p.a.), not a
quote, and not calibrated to any market. Financial execution is a separate
concern from recommendation and is never implied by an offer.

---

## 2. Architecture

Layers are listed in dependency order. Each one is importable without the one
below it, which is what makes the serving/training split in §5 enforceable.

```
domain/        Frozen types: merchants, obligations, payment events, shocks,
               interventions, propagation results. Pydantic, immutable.
graph/         TemporalPaymentGraph - event-level multigraph plus a separate
               dependency overlay. Ground-truth and estimated edges never mix.
data/          ORM schema, mappers, repositories, unit of work; the synthetic
               generator (build-time only - see §5).
simulation/    Discrete-event liquidity simulator + counterfactual evaluator.
               Common Random Numbers keyed on (run_seed, stream, obligation_id).
models/        Dependency estimation (marked-Hawkes EM), analytic propagation,
               the GATv2 temporal graph model.
benchmark/     Scenario families, scales, ground-truth definitions, validation
               invariants. FROZEN as of Phase 2.
learning/      Phase 3. The leakage barrier, leak-free features, temporal
               splits, baselines, calibration, evaluation, ablations.
intervention/  Phase 4. Typed actions, feasibility, exact/MILP/scalable solvers,
               robustness, counterfactual evaluation, resource profiles.
inference/     Content-addressed model artifacts; load-once predictor service.
snapshot/      Phase 5. Precomputed analytical snapshot + bounded on-demand
               analysis + the frontend data contract.
execution/     Provider abstraction. SimulationProvider and RazorpayTestProvider.
razorpay/      Client, webhook verification, ingestion.
api/           FastAPI. Analytics routers (frontend) + dev routers (datasets).
```

### The three things worth knowing before touching it

**1. The leakage barrier is a single function.**
`lce.learning.problem.is_observed(t, origin, cutoff)` is the only place the
temporal cutoff is decided. It is factored out precisely so a test can
monkeypatch it and prove the leakage audit fires. Latent generator parameters
(`Θ`, `payment_discipline`, `λ`, `μ`, `systemic_weight`), post-origin events and
the shock-perturbed obligation book are structurally unreachable from a feature
table — `scrub_profile` replaces them with neutral values before a model sees a
merchant.

**2. Interventions are scored by the simulator, never by the model.**
The optimiser's objective is a black box that replays the action in the true
simulator. A model prediction may *propose* a candidate; it never *scores* one.
`exact_optimum` is complete enumeration over the union of every strategy's
actions, evaluated by simulation — that is the scientific reference in §6, and
the learned model is not it.

**3. A frontend read never triggers optimisation.**
Everything the dashboard shows comes from a snapshot built offline. On-demand
analysis exists (`POST /scenarios/analyze`) but is bounded: ≤5 merchants,
≤12 candidates, ≤2 actions, and refused outright above 2000 merchants.

---

## 3. Frontend-facing API

Base: `http://127.0.0.1:8000/api/v1`. All responses are Pydantic models with
`extra="forbid"`, so the schema is a contract, not a suggestion.

Every response that reports a *computed* result carries a `provenance` block —
run ID, scenario ID, dataset ID and version, model version, feature schema
version, seed, config hash, simulator config hash, optimizer, code version,
created-at. That covers `/scenarios/{id}` and its sub-resources,
`/network/systemic-importance`, `/offers*` and `/dashboard`. Descriptive reads
(`/network`, `/network/merchants`, `/network/dependencies`) carry
`dataset_id`/`dataset_version` instead, and `/snapshot` carries the snapshot's
full identity (ID, dataset version, seed, content hash, code version) as
top-level fields.

### Network

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/network` | Size, value, sector/tier composition |
| GET | `/network/merchants` | All merchant profiles with exposure |
| GET | `/network/merchants/{merchant_id}` | One merchant |
| GET | `/network/dependencies` | Estimated dependency relationships, ranked |
| GET | `/network/systemic-importance` | SI ranking + centrality baseline correlations |

### Scenarios

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/scenarios` | Scenario summaries |
| GET | `/scenarios/{scenario_id}` | Full analytical snapshot |
| GET | `/scenarios/{scenario_id}/impact` | Projected impact + time-to-constraint distribution |
| GET | `/scenarios/{scenario_id}/interventions` | Ranked intervention options |
| GET | `/scenarios/{scenario_id}/counterfactual` | Before/after, measured by replay |
| POST | `/scenarios/analyze` | Bounded on-demand analysis of a chosen shock |
| GET | `/scenarios/analyze/limits` | The caps, so a client can pre-validate before a 422 |
| POST | `/scenarios/replay` | Replay a specific intervention set |

### Offers and operations

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/offers` | All offer contracts |
| GET | `/offers/{merchant_id}` | Offer for one merchant |
| GET | `/dashboard` | The admin data contract (§8) |
| GET | `/execution/status` | Probed provider capabilities |
| GET | `/snapshot` | Snapshot manifest and health |
| GET | `/model` | Model artifact metadata |
| POST | `/predict/contagion` | Versioned contagion prediction |
| POST | `/interventions/recommend` | Versioned intervention recommendation |
| GET | `/health`, `/health/live`, `/health/ready`, `/health/config` | Liveness/readiness |

### Not exposed, deliberately

There is no play/stop control, no timestamp slider, no tick endpoint, no event
stream. The simulator is an implementation detail of how an analytical result is
computed. `tests/test_snapshot.py` scans every response model for simulator
vocabulary and fails if it appears in the contract.

### Developer surface (not for the frontend)

`/datasets/*`, `/runs/*`, `/shocks/*`, `/webhooks/*` are development and
ingestion endpoints. They are the only routes that can reach the dataset
generator, and they do so through a function-local import (§5).

---

## 4. Model artifact

| Field | Value |
| --- | --- |
| Name | `contagion_hazard` |
| Model version | `discrete_hazard-9d1ffe99e9` |
| Kind | Discrete-time hazard (survival) |
| Feature schema | `phase3-node-v1` |
| Design width | 52 (38 node + 4 interval columns, expanded over 8 hazard intervals) |
| Calibrator | Isotonic (PAVA), fitted on validation only |
| Content hash | `d99a0b97feb42050` (SHA-256 prefix) |
| Training dataset | `synth-06d426b44024` |
| Code version | 0.1.0 |
| Path | `artifacts/contagion` |

Loading verifies, in order: artifact format version → content hash → feature
schema version. A mismatch on any of the three is refused rather than
tolerated, because a silently-wrong feature order is a wrong prediction that
looks fine.

The design matrix is built by `lce.learning.features.build_hazard_design`, which
lives in the feature module rather than the model so that training and serving
construct byte-identical designs. This is the single most important line of
defence against training/serving skew in the system.

---

## 5. Training is not on the serving path

**Claim:** a process that answers frontend requests contains no data generator,
no benchmark package, and no training code.

**How it is enforced:** `tests/test_inference.py` imports the serving stack
(`lce.snapshot.store`, `lce.snapshot.dashboard`, `lce.api.routers.analytics`,
`lce.api.app`) in a clean interpreter and fails if any of `lce.data.generator`,
`lce.benchmark.*`, `lce.learning.dataset`, `lce.learning.baselines`,
`lce.learning.experiment` or `lce.intervention.experiment` appears in
`sys.modules`. `src/lce/scripts/audit_backend.py` runs the same check.

**What this cost:** the narrower version of this test (inference package only)
passed for two phases while `lce.api.app` still pulled the generator in through
three separate package `__init__` re-exports — `lce.data`, `lce.experiments`,
and the analytics router's transitive path. `lce.data`, `lce.experiments` and
`lce.learning` now resolve public names lazily through `__getattr__`, and the
two genuine generator call sites (`NetworkService.generate_and_store`, the
`POST /datasets` handler) import it inside the function. The assertion is now
made against the module a request actually enters, which is the only version of
it that means anything.

Serving-path modules also carry `lce.snapshot.views` — the deserialisation and
view helpers shared with the offline builder — specifically so `lce.snapshot.build`
(which does reach the generator, the benchmark package and the Phase-4
experiment runner) is never imported by anything that serves.

---

## 6. Benchmark results

All figures are on synthetic networks with known ground truth. Confidence
intervals are clustered bootstraps that resample **scenarios**, not nodes,
because nodes within a scenario are not independent.

### 6.1 Contagion prediction (Phase 3)

60 datasets, 420 scenario-examples, 42,000 scored nodes, 7 shock families.
Temporal split: 252 train / 84 validation / 84 test, with a 1,485-hour purge
band between validation and test. Split audit: 7/7 checks clean.

| Model | PR-AUC | 95% CI | Downstream PR-AUC | ROC-AUC | ECE | Timing MAE (h) |
| --- | --- | --- | --- | --- | --- | --- |
| `discrete_hazard` | **0.857** | [0.768, 0.905] | 0.201 | 0.969 | 0.0027 | 20.9 |
| `temporal_gnn` | 0.844 | [0.741, 0.883] | **0.335** | **0.977** | 0.0025 | 21.5 |
| `cash_cover` | 0.719 | [0.625, 0.801] | 0.014 | 0.931 | 0.0019 | **15.6** |
| `hawkes_linear` | 0.709 | [0.648, 0.782] | 0.126 | 0.935 | 0.0023 | 33.9 |
| `shock_distance` | 0.703 | [0.587, 0.813] | 0.078 | 0.973 | 0.0024 | 26.5 |
| `hawkes_cascade` | 0.656 | [0.574, 0.773] | 0.032 | 0.970 | 0.0044 | 34.6 |
| `prevalence` | 0.015 | [0.012, 0.021] | 0.005 | 0.500 | 0.0006 | 32.2 |

**Read the downstream column, not the pooled one.** Pooled PR-AUC is dominated
by shock origins, which are trivially predictable. The downstream view excludes
them and is the honest measure of whether contagion — as opposed to the shock
itself — was predicted. On that measure the graph model is ahead
(0.335 vs 0.201) and the confidence intervals on the pooled figures overlap
heavily, so `discrete_hazard` leading the pooled table is not a claim that it is
the better model.

An earlier 24-seed run produced a materially different ordering (`hawkes_linear`
0.440 → 0.126 at 60 seeds). That instability is the reason the clustered
bootstrap exists and why single-run orderings are not reported as findings here.

### 6.2 Ablations

Deltas are against the same model without the ablation, on the same test block.

| Ablation | Leaky | PR-AUC | Δ | Downstream | Δ | Question |
| --- | --- | --- | --- | --- | --- | --- |
| `no_graph` | no | 0.757 | −0.100 | 0.010 | **−0.191** | Is the network worth anything over per-merchant features? |
| `true_structure` | **yes** | 0.861 | +0.017 | 0.298 | −0.037 | How much of the gap is dependency-estimation error? |
| `shuffled_edges` | no | 0.853 | +0.009 | 0.281 | −0.054 | Negative control: same degrees, destroyed topology |
| `no_edges` | no | 0.867 | +0.023 | 0.255 | −0.080 | Graph neural network, or just neural network? |

`no_graph` is the informative row: removing network features costs 95% of
downstream discrimination. The `true_structure` row is an upper bound, not a
result — it conditions on hidden variables.

`shuffled_edges` scoring close to the real topology is a genuine caveat, not a
success: at this scale the model is drawing much of its signal from node
features and degree, and the topology contributes less than the `no_graph`
delta alone would suggest.

### 6.3 Unsupervised dependency recovery

Estimated from observable transaction history alone (marked-Hawkes EM), scored
against withheld latent ground truth.

| Metric | Unsupervised | Supervised upper bound |
| --- | --- | --- |
| Pass-through correlation | **0.703** | 0.463 |
| Pass-through Spearman | **0.746** | 0.495 |
| Pass-through MAE | **0.098** | 0.150 |
| Pass-through bias | −0.001 | −0.003 |
| Edge precision / recall | 1.000 / 0.997 | — |
| Lag Spearman | 0.450 | — |
| Lag MAE (hours) | 37.8 | — |

The unsupervised estimator beats the supervised regression on every
pass-through metric. That is not a paradox: the supervised model is a pooled
cross-sectional regression on pair features, while the EM estimator uses the
event timing directly. It is reported as the upper bound it was built to be,
and it is a weak one.

Conditional-probability recovery is poor (corr 0.236, MAE 0.442) and the lag law
is recovered only ordinally. Both are stated rather than smoothed over.

### 6.4 Intervention (Phase 4)

7 scenarios, `SMALL_FAST` profile, penalised objective (λ=1.0), ≤2 actions,
≤12 candidates. `exact_optimum` is complete enumeration evaluated in the true
simulator.

| Strategy | Mean reduction % | Mean cost | Capital efficiency | Relative gap | Infeasible | Runtime (s) |
| --- | --- | --- | --- | --- | --- | --- |
| `exact_optimum` | **45.4** | 2.75e8 | 8.70 | 0.000 | 0 | 2.01 |
| `model_guided_milp` | 35.5 | 5.71e7 | 28.02 | 0.040 | 1 | 1.06 |
| `model_guided_greedy` | 35.5 | 5.71e7 | **27.99** | 0.040 | 0 | 0.38 |
| `model_guided_robust` | 35.5 | 5.71e7 | 27.99 | 0.040 | 0 | 0.72 |
| `cash_cover` | 31.7 | 1.62e8 | 6.28 | 0.228 | 0 | 0.02 |
| `highest_systemic_importance` | 8.2 | 2.39e8 | 1.07 | 0.725 | 0 | 0.02 |
| `naive_largest_deficit` | 8.2 | 2.39e8 | 1.07 | 0.725 | 0 | 0.02 |
| `highest_degree` | 0.0 | 7.86e7 | 0.00 | 0.769 | 0 | 0.02 |

The model-guided strategies reach 78% of the optimum's disruption reduction for
21% of its capital — a 3.2× capital-efficiency advantage. `highest_degree`
reduces nothing while spending 7.9e7, which is the case for not ranking by
centrality.

### 6.5 Intervention, acceptance-harness run (14 scenarios)

A second, larger run from `verify_phase4.py` — 4 seeds, 14 scenarios, same
profile. It is reported separately rather than merged, because the numbers move:

| Strategy | Mean reduction % | Mean cost | Capital efficiency | Relative gap | Infeasible |
| --- | --- | --- | --- | --- | --- |
| `exact_optimum` | **55.7** | 2.33e8 | 13.50 | 0.000 | 0 |
| `model_guided_milp` | 37.9 | 5.03e7 | 36.62 | 0.334 | **2** |
| `model_guided_robust` | 37.9 | 5.03e7 | 36.62 | 0.334 | **1** |
| `model_guided_greedy` | 37.9 | 5.03e7 | **36.62** | 0.334 | **0** |
| `cash_cover` | 27.2 | 1.24e8 | 8.43 | 0.681 | 0 |
| `highest_systemic_importance` | 26.0 | 1.97e8 | 6.31 | 0.660 | 0 |
| `naive_largest_deficit` | 26.0 | 1.97e8 | 6.31 | 0.660 | 0 |
| `highest_degree` | 2.9 | 5.87e7 | 0.00 | 1.181 | 0 |
| `no_intervention` | 0.0 | 0 | — | 1.213 | 0 |

Two results here matter more than the headline:

**The optimality gap is scenario-dependent and can be large.** Greedy's mean
relative gap is 0.040 over the 7 snapshot scenarios and 0.334 over these 14.
The 0.040 figure is not the system's general accuracy; 0.334 is closer to what
should be assumed on unseen scenarios.

**MILP and robust each produced infeasible plans; greedy did not** (2 and 1 out
of 14, versus 0). The MILP optimises a *measured pairwise surrogate*
(r_ij = g_i + g_j − g_ij) and then re-simulates; when the surrogate's additivity
assumption fails the violation is reported rather than hidden. Greedy searches
the true objective directly. **Greedy is the recommended production strategy**
for that reason, not for its runtime.

**Candidate pruning is not safe in general.** The harness benchmarks pruning on
two probes. One retained the optimum (39 → 11 candidates, relative regret 0.000,
runtime −90%). The other did not (45 → 12 candidates, **optimum lost, relative
regret 0.981**, runtime −93%). Pruning is a measured speed/quality trade, and on
that second probe the trade was bad. The bound is reported per run rather than
assumed, and this is why the exact optimum is computed as ground truth instead
of trusting the pruned set.

### 6.6 Systemic importance

SI_i = D(G, shock_i) − D(G, no_shock), measured by simulation over a 40-merchant
sample. Rank correlation against centrality baselines: degree 0.834,
throughput 0.826, cash deficit 0.664. Correlated but not equivalent — and §6.4
shows ranking by degree directly is worthless as an intervention policy.
A second dataset gave throughput 0.80, degree 0.73, deficit 0.75.

---

## 7. Razorpay: what was actually verified

Mode `test`. Credentials read from `.env` via pydantic-settings and never
logged, echoed, or included in any payload.

Capabilities are established by **calling the endpoint an action would use**,
not by reading configuration. Probed live on this account:

| Capability | Probed result |
| --- | --- |
| `api_reachable` | ✅ true |
| `payments` | ✅ true |
| `orders` | ✅ true |
| `settlements` | ✅ true |
| `transfers` | ❌ false |
| `route_accounts` | ❌ false |
| `credit_ledger` | ❌ false |
| `term_ledger` | ❌ false |

**Route / Direct Transfers are not enabled on this account.** Consequently
`executable_intervention_types` is `[]` and every recommended action is recorded
as a plan through the `SimulationProvider` fallback. This is reported in
`GET /execution/status`, not assumed anywhere, and the system was never written
to assume Route exists.

No funds move under any code path. Live mode is refused at construction:
`RazorpayTestProvider` raises `ConfigError` if `RAZORPAY_MODE=live`, and the
audit asserts the refusal.

Live tests are opt-in. `pyproject.toml` sets
`addopts = "-q --strict-markers -m 'not razorpay_live'"`, so the default run is
fully offline; 3 live tests run only when explicitly selected (§9).

`RAZORPAY_WEBHOOK_SECRET` is not configured in this environment, so webhook
signature verification is exercised only by the mocked tests.

---

## 8. Admin data contract

`GET /api/v1/dashboard`. Every value is computed from the loaded snapshot or
probed at call time; nothing is hardcoded, and a value that cannot be computed
is `null` rather than a plausible-looking number. Current canonical snapshot:

| Field | Value |
| --- | --- |
| Merchants | 100 |
| Transacting merchant pairs | 216 |
| Payment events (history) | 11,413 |
| Total payment value | ₹58,749,728,979 |
| Obligation value in horizon | ₹1,783,270,241 |
| Merchants vulnerable | 10 (10.0%) |
| Total value exposed | ₹873,233,889 |
| Mean shock reach (scored share) | 0.0114 |
| Projected disrupted value | ₹16,333,385 |
| Intervention opportunities | 4 |
| Best capital efficiency | 25.27 |
| Total recommended capital | ₹105,212,876 |
| Recommended offer | `m0005`, ₹77,269,031, liquidity injection, indicative 14.60% p.a., eligible |

Two field names deserve care and are documented in the model itself:

- `total_value_exposed` is obligation value held by merchants whose cover ratio
  is below one. It is not a loss estimate and not a prediction.
- `mean_failure_probability` is the mean share of the scored network that shocks
  actually reached **in simulation**. It is a measured frequency, not a
  calibrated probability of anything happening in the world.

`ProjectedImpact` reports both `disruption_index` (the shock's *attributable*
contribution, `D(shocked) − D(no_shock)`) and `network_disruption_index` (the
whole network's disruption under the shock). Collapsing them let a scenario that
reached nobody appear 40% mitigated; they are now separate fields with
docstrings that say which is which.

---

## 9. Verification

All commands run from the repository root.

```bash
PYTHONPATH=src python -m pytest
```
**438 passed, 3 deselected** (the deselected 3 are the opt-in live Razorpay
tests). Fully offline; no network access, no credentials required.

```bash
python -m ruff check src tests
```
**All checks passed.**

```bash
PYTHONPATH=src python -m mypy src/lce
```
**Success: no issues found in 129 source files.**

```bash
PYTHONPATH=src python src/lce/scripts/audit_backend.py
```
**22 checks clean** — secrets, `.env` ignored, serving-import separation,
artifact integrity, snapshot provenance, bounded on-demand analysis,
determinism, route completeness, migration chain, live-mode refusal.

```bash
PYTHONPATH=src python src/lce/scripts/audit_backend.py --live
```
**24 checks clean** — adds the live Test Mode reachability probe and the
assertion that Route availability was probed rather than assumed.

```bash
PYTHONPATH=src python -m pytest -m razorpay_live -v
```
**3 passed** against the real Razorpay Test Mode API.

```bash
PYTHONPATH=src python src/lce/scripts/verify_phase4.py
```
**28 checks pass** — exact-optimum dominance over every feasible strategy,
no money creation, feasibility of recommendations, reproducibility of a
decision under the same seed, pruning benchmark, provenance completeness.
Runtime ~4 minutes.

---

## 10. Running the backend

### First time

```bash
python -m venv .venv && .venv/Scripts/activate && pip install -e ".[dev]"
```

```bash
cp .env.example .env
```

Then fill in `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` and optionally
`RAZORPAY_WEBHOOK_SECRET`. `.env` is gitignored and must stay that way.

### Build the analytical artifacts (offline, once)

```bash
PYTHONPATH=src python -m lce.cli snapshot build --seed 2025 --scale small --out artifacts/snapshots
```

The model artifact at `artifacts/contagion` is already built and content-hashed;
rebuild it only via the Phase-3 pipeline if the feature schema changes.

### Start the API

```bash
PYTHONPATH=src python -m lce.cli serve --host 127.0.0.1 --port 8000
```

The snapshot and the model artifact load once at startup. `/health/ready`
covers the database; snapshot and artifact status are reported by
`GET /snapshot` and `GET /model`, each of which carries its own `status`
field and load time.

### Confirm it is up

```bash
curl -s http://127.0.0.1:8000/api/v1/health/ready
```

---

## 11. Frontend integration

### Schema

```bash
curl -s http://127.0.0.1:8000/openapi.json > openapi.json
```

Generate a typed client from that file. Response models are `extra="forbid"`,
so an unexpected field is a server bug, not a client concern. Interactive docs:
`http://127.0.0.1:8000/docs`.

### The three calls a dashboard needs

```bash
curl -s http://127.0.0.1:8000/api/v1/dashboard
```

```bash
curl -s http://127.0.0.1:8000/api/v1/scenarios
```

```bash
curl -s http://127.0.0.1:8000/api/v1/scenarios/<scenario_id>
```

`/dashboard` is one read for the whole admin view. `/scenarios/{id}` returns the
complete analytical snapshot for one shock: description, affected merchants,
projected impact, time-to-constraint distribution, cascade depth, systemic
exposure, recommended intervention, ranked alternatives, counterfactual outcome,
confidence, and provenance.

### Before calling `/scenarios/analyze`

```bash
curl -s http://127.0.0.1:8000/api/v1/scenarios/analyze/limits
```

Read the caps and validate client-side. Requests over them are refused with 422,
not silently truncated.

### CORS

Set `LCE_CORS_ORIGINS` to the frontend origin before starting the server.

---

## 12. Limitations

Stated plainly, because a frozen system with unstated limits is worse than an
unfrozen one.

**Scientific**

1. Every performance number is on synthetic data with known ground truth. None
   of it is evidence about live merchant behaviour.
2. `shuffled_edges` scores close to the true topology (§6.2). At this scale much
   of the signal is node-level, and the graph contribution is smaller than the
   `no_graph` delta implies.
3. Dependency **conditional probabilities** are recovered poorly (corr 0.236);
   only pass-through and ordinal lag structure are recovered well.
4. Downstream positive rate is 0.4%. Downstream PR-AUC confidence intervals are
   correspondingly wide — `discrete_hazard`'s is [0.001, 1.000] at the artifact's
   4-seed training scale.
5. The Phase-3 leaderboard ordering is not stable across seed counts. Only the
   clustered-bootstrap intervals should be quoted.
6. Systemic importance is measured on a 40-merchant sample, not the full network.

**Engineering**

7. Exact optimisation is complete enumeration. It is the scientific reference at
   `SMALL_FAST` scale (≤2 actions, ≤12 candidates) and does not scale; larger
   networks use the two-stage procedure with a measured optimality gap. This is
   the documented scaling boundary, not a hidden one.
8. `model_guided_milp` and `model_guided_robust` produce occasional infeasible
   plans (2 and 1 of 14 in §6.5). Use greedy, which produced none.
9. The optimality gap is scenario-dependent: greedy averaged 0.040 over the 7
   snapshot scenarios but 0.334 over the 14 acceptance scenarios. Assume the
   larger figure on unseen scenarios.
10. Candidate pruning lost the optimum on one of two benchmark probes (relative
    regret 0.981, §6.5). It is a measured trade-off, not a safe optimisation.
11. On-demand analysis is refused above 2,000 merchants. Larger networks must be
    analysed offline into a snapshot.
12. The canonical snapshot is `small` scale (100 merchants). `medium` and
    `large_demo` profiles exist and are untested at freeze time.

**Integration**

13. Route / Direct Transfers are not enabled on the test account, so no
    intervention type is executable and every action is a plan.
14. `RAZORPAY_WEBHOOK_SECRET` is unset here; webhook signature verification is
    covered only by mocked tests.
15. `DATABASE_URL` defaults to SQLite locally. The migration chain
    (`0001` → `0002_provenance`) is Postgres-targeted and has a single head.

---

## 13. Reproducibility

Every analytical result carries: run ID, scenario ID, dataset ID and version,
model version, feature schema version, seed, config hash, simulator config hash,
optimizer identity, code version, created-at.

The canonical demo is reproducible offline from the two commands in §10 with no
network access. Snapshots and model artifacts are content-addressed; a
`content_hash` mismatch on load is refused rather than tolerated.

Canonical snapshot: `snap-a4c99d5a49a64ffd`, dataset `synth-eab9b3bbb256`,
seed 2025, scale `small`, 7 scenarios, content hash `668c4a7b6d6fb71c`.
Model artifact: `discrete_hazard-9d1ffe99e9`, content hash `d99a0b97feb42050`.

Phase reports are retained at `reports/phase3.json` and
`reports/phase4/<run_id>/result.json`.

---

## 14. Freeze declaration

The backend is **FROZEN**.

Do not modify the generator, simulator, ground-truth definitions, scenario
families, validation invariants, the leakage barrier, the feature schema, or any
response model without a test that demonstrates a genuine defect. The feature
schema version and artifact content hash exist to make an accidental change
loud rather than silent.

Frontend development can begin against `openapi.json`.
