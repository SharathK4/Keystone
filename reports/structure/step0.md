# Structure (Edge · Horizon · Fog) — Gate 0 / Step 0

**Status: STOPPED at Gate 0.** Two of the brief's stop conditions are triggered
(Edge §4 linearisation, and a Horizon premise that does not hold on the frozen
artifact). Nothing in `src/` has been changed. All numbers below come from the
read-only probes in [`probes/`](probes/), each with its captured output.

Date: 2026-10-21 · branch `paypal` · HEAD `0a699b0`

---

## Baseline (§0.3) — matches the brief exactly

| Check | Expected | Measured |
| --- | --- | --- |
| `pytest` | 438 passed / 3 deselected | **438 passed, 3 deselected** (95 s) |
| `ruff check src tests` | clean | **All checks passed** |
| `mypy src` | no issues | **Success: no issues found in 129 source files** |
| `audit_backend.py` | 22 checks clean | **BACKEND AUDIT CLEAN (22 checks)** |
| `verify_phase4.py` | 28 checks | **ALL PHASE-4 ACCEPTANCE CRITERIA HOLD (28 checks)** |
| Snapshot anchor | `668c4a7b6d6fb71c` | `artifacts/snapshots/snap-a4c99d5a49a64ffd/manifest.json` → `668c4a7b6d6fb71c…` ✓ |
| Model anchor | `d99a0b97feb42050` | `artifacts/contagion/manifest.json` → `d99a0b97feb42050…` ✓ |

Environment difference to note: **Python 3.13.1** here (brief assumes 3.12;
`pyproject.toml` allows `>=3.12`). Everything above passes on 3.13.

---

## (a) Estimator and analytic propagation

**There are two propagators, and the snapshot uses the one that is not linear.**

`DependencyEdge` (`src/lce/domain/edges.py:183`) carries `pass_through`,
`conditional_probability`, `reliability`, a `lag: LagDistribution`, Hawkes
`excitation_alpha` / `excitation_decay` / `base_intensity`, `is_ground_truth`
and `estimator`. The marked-Hawkes EM fit (`src/lce/models/hawkes.py:53`,
`MarkedHawkesFit`) returns `mu, alpha, beta, theta, sigma_ratio,
conditional_probability, lag_mu_log, lag_sigma_log, lag_weight,
excitation_mass, background_mass, …` per edge.

1. **`LinearThresholdPropagator`** (`src/lce/models/propagation.py:121`) — what
   the snapshot uses (`src/lce/snapshot/analysis.py:150`, `source="propagation"`).
   It carries **rupee shortfall**, not events. Per hop:
   `excess = max(0, incoming − buffer) − already_propagated` (a ReLU on the
   buffer), split by normalised pass-through shares, **capped by the live
   obligation amount** (`min`), multiplied by `1 + (1 − reliability)`, dropped
   below `min_transmit`, and gated by `lag.mean_hours` landing inside the
   horizon. A merchant is "constrained" when
   `logistic(k·(received/buffer − 1)) ≥ 0.5`, i.e. when received ≥ buffer.
2. **`HawkesCascadePredictor`** (`propagation.py:252`) — probability propagation:
   `p_edge = p_source · conditional_probability · lag.cdf(H − t_source)`,
   combined across parents by noisy-OR. Not used by the snapshot; selectable in
   `services/analysis_service.py`.

### Edge stop condition (§4) — measured, `probes/probe_edge.out`

| Candidate `N` | Definition | ρ on canonical overlay | Matches its propagator? |
| --- | --- | --- | --- |
| **L1** Hawkes, rare-event | `N_ij = c_ij · F_ij(168 h)` | **0.283** (211 nnz, not nilpotent) | single-origin scenarios: **≤ 4.6e-8** · 3-origin scenario: **8.8e-2** |
| **L2** threshold, saturated | `M_ij = share_ij · (2 − rel_ij)` | **0.864** | none — it ignores buffer absorption, so it is an upper bound only |

- **E2 fails as written.** L1 is the *exact* first generation of the Hawkes
  predictor whenever one merchant is shocked (residual = the predictor's own
  1e-6 cut-off), but with several origins the predictor combines paths by
  noisy-OR while `N` sums them; the gap is 0.088 on `multi_node_shock` because
  entries are not small (max 0.86). L1 is exact for *expected counts* — which is
  what §4 defines `N` as — but the predictor reports *P(at least one)*. Treating
  that as passing would be loosening the gate after seeing the result.
- **No linearisation exists for the propagator the snapshot actually uses.** Its
  derivative is 0 below a merchant's buffer and `M` above it.
- **E4 has a structural problem with L1.** L1 contains no buffer term, so a
  liquidity scale κ applied to merchant buffers cannot move ρ. It can only move
  through re-estimation if κ is applied **through the generator** — which is
  possible additively: `scale_config(overrides=…)` already accepts
  `coverage_low`, `coverage_high`, `credit_line_ratio` and
  `operating_floor_ratio` (`src/lce/data/generator.py:225-230`,
  `src/lce/benchmark/scales.py:129`). Cost: regenerate + Hawkes EM + replays per
  grid point.

## (b) Hazard model and time-to-constraint

`HazardPredictor.predict` (`src/lce/inference/predictor.py:250`) returns, per
merchant, `probability_constrained` (isotonic-calibrated),
`expected_time_to_constraint_hours` (mean of the implied density) and
`probability_by` (the CDF on a grid). The 8 hazard intervals are equal splits
of the **remaining** horizon after the observation cutoff
(`task.interval_edges(window.remaining_hours)`), e.g. `[0,19,…,149]` for a
cutoff at 18.8 h and `[0,8,…,61]` for a cutoff at 107.4 h.

`/scenarios/{id}/impact` (`src/lce/api/routers/analytics.py:192`) does **not**
use the hazard model. Its `time_to_constraint` is a histogram of the
**propagator's point hit times** over fixed buckets
`(0, 6, 12, 24, 48, 72, 120, 168)` (`src/lce/snapshot/views.py:43,124`).
The canonical snapshot's `confidence.model_version` is `null`.

### Horizon premise does not hold — measured, `probes/probe_hazard.out`

Serving all seven canonical scenarios through the production inference path
(mirroring `intervention.experiment.predict_with_artifact`):

- **Every non-origin merchant gets `probability_constrained ≈ 0.000`** in every
  scenario. Only the shocked merchants reach P = 1.000.
- The artifact's manifest threshold is **1.0**, consistent with the known 0.4 %
  downstream positive rate.
- `expected_time_to_constraint_hours` for those P≈0 merchants is a small,
  meaningless number (0.1–0.9 h), because the code only falls back to the last
  midpoint when the density mass is below 1e-12. **H5 is violated by the raw
  output**: a merchant that is never constrained gets a number, not null.

So a Horizon built on hazard `T_j` (as §5 specifies) would make every
downstream merchant "reachable" at every latency — a flat value-of-speed curve
that is technically true and carries no information.

### What *does* carry signal — measured, `probes/probe_latency.out`

Replaying each canonical scenario's **selected plan** with every action landing
`δ` hours later (true simulator, Common Random Numbers, nothing else changed):

| δ | commerce kept | protected | disruption cut |
| --- | --- | --- | --- |
| 0 h | ₹12.18 Cr | 1 | 40.36 % |
| 1 h | ₹12.18 Cr | 1 | 40.28 % |
| 6 h | ₹10.48 Cr | 0 | 39.73 % |
| 12 h | ₹4.54 Cr | 0 | 37.78 % |
| 24 h | ₹2.65 Cr | 0 | 32.93 % |
| 48 h | ₹0.00 | 0 | 20.93 % |
| 72 h | ₹0.00 | 0 | 3.69 % |

`single_missed_inflow`; `delayed_inflow` and `partial_payment` are within 0.1 pp
(same plan). δ = 0 reproduces the snapshot's counterfactual exactly
(₹12.18 Cr, 40.36 %), so the probe is consistent with the frozen artifact.
`multi_node_shock` is flat near zero **and non-monotone** (2.44 % at 0 h,
3.12 % at 6 h) — reported, not smoothed. Three scenarios have no plan.

## (c) Timed actions in replay — **YES**

`Intervention.t` is "when the intervention takes effect"
(`src/lce/domain/intervention.py:55`). `LiquiditySimulator.run` sorts actions by
`t` and, each tick, applies every action with `t < t1` **before** inflows,
shock and settlement (`src/lce/simulation/engine.py:306-331, 339-349`).
Resolution is one tick (`tick_hours = 1.0`). There is **no** configurable
execution-delay parameter, but none is needed: a delay δ is `t = t0 + δ` at
construction, which is what `probe_latency.py` does with `model_copy`.

## (d) Liquidity scale without editing frozen code — **YES**, two ways

1. **Perturbed copy of a built graph**: `TemporalPaymentGraph` has public
   `add_merchant(s)`, `add_payments`, `add_obligations`, `from_payload`
   (`src/lce/graph/temporal_graph.py:105-237, 405`), and `MerchantProfile`
   (`src/lce/domain/merchant.py:48`) exposes `opening_balance`, `credit_limit`,
   `operating_floor`. `model_copy(update=…)` gives scaled profiles. History is
   unchanged, so this moves the threshold propagator and the simulator but not
   the Hawkes estimate.
2. **Through the generator**: `scale_config(overrides=…)` (existing public
   argument, also used by `build_snapshot(generator_overrides=…)`) accepts
   `coverage_low`, `coverage_high`, `credit_line_ratio`. History is regenerated,
   so estimates move too.

`SimulationConfig` (`engine.py:88`) holds horizon, tick, grace period,
settlement lag, partial payments, payment discipline and seed — no buffer scale.

## (e) Snapshot build, store, views, provenance

- **Build** (`src/lce/snapshot/build.py:546`): regenerates the network from
  `(seed, scale)` with `generate_network(scale_config(...))`, runs systemic
  importance and `run_scenario` per scenario, installs the *estimated* overlay
  from the first scenario's observed window into a serving graph with payment
  events stripped, and emits a payload + `SnapshotManifest`.
- **Store** (`src/lce/snapshot/store.py`): `save_snapshot`, `resolve_snapshot`,
  `SnapshotStore` (verifies identity on load), `get_store` / `require_store`.
- **Views** (`src/lce/snapshot/views.py`): pure functions from build results to
  `extra="forbid"` contract models in `src/lce/snapshot/models.py`; serving
  never imports `build.py` (the Phase-5 separation).
- **Provenance**: one `Provenance` per build (`run_id = snap-{config_hash}`,
  dataset id/version, seed, config hash, simulator config hash, optimiser,
  code version, created_at), copied per scenario with `scenario_id`.
- A structure artifact can mirror this exactly: `lce structure build` →
  payload + manifest referencing `snapshot_id`/`dataset_version`, own
  `content_hash`, verified on load.

## (f) Simulator-vocabulary scan

`tests/test_snapshot.py:48`:
`FORBIDDEN_FIELD_TOKENS = ('tick', 'timeline', 'cursor', 'playing', 'play_state',
'is_paused', 'current_time', 'step_index', 'frame')` — a **substring** match on
lower-cased field names (`:489`). New model fields must avoid them (e.g. no
`*_frame`, `ticker`, `timeline_*`).

## (g) Frontend

- `src/api/client.ts` — the only network access; every GET cached by URL with
  in-flight sharing; failures are not cached and surface as `ApiError`.
- `src/lib/format.ts` — `inr()`/`inrParts()`/`inrExact()` for rupees, `idx()`
  for the objective index (no currency sign), `ratio()`, `hours()`, `pct()`.
- `src/lib/offerCopy.ts` — copy keyed on scenario family, then intervention type.
- `TexturedTile` — one `feTurbulence` construction, four colourways
  (`ink`/`slate`/`clay`/`ember`), seeded per id.
- Chord diagram — `NetworkVisualization` + `lib/networkLayout.ts`; annotation
  drawn in SVG user units; no ambient motion.
- Hundred-dot grid — `ExposureDotMatrix` (position = cover ratio, ink = shock).
- Hash routing — `KeystonePage.tsx` (`#network`, `#exposure`, `#intervention`,
  `#offer`) seeding a state transition into a portalled `ExpandedAnalysis`.
- "Method" — the `XSection label="How this was measured"` block at the end of
  `InterventionExpanded.tsx`.
- **Copy rule conflict**: brief §8.2 bans "simulator", "engine" and "risk" in UI
  copy. The current UI uses "simulator" in **5 user-visible strings**
  (`InterventionExpanded.tsx` ×4 including the "Simulator config" label,
  `OfferExpanded.tsx` ×1). Fixing is trivial but it is a frontend change.

---

## Decisions needed before Gate 1

1. **Edge** — E2 fails as written. Choose: (i) define `N` on the Hawkes predictor
   (exact for expected counts; noisy-OR gap reported as a stated approximation;
   κ applied through the generator), (ii) keep Edge as a reported-but-unshipped
   research note, or (iii) cut it.
2. **Horizon** — the hazard `T_j` premise fails on the frozen artifact. Proposed
   replacement: **Horizon = the measured decay of the selected plan's value with
   execution latency δ**, by replay with CRN on a pre-registered δ grid. It needs
   no frozen-code change and no model, and it is the most PayPal-central result
   in the repository. Validation becomes replay-only (monotonicity reported,
   clustered bootstrap across a larger scenario set).
3. **Fog** — not started; see `reports/PLAN.md` for the recommendation to defer.
