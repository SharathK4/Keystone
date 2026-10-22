# Gate H — the value of speed

**Replayed offline, inside the synthetic generator.** Delays are scenario
parameters (hours between the moment the shock is known and the moment the money
lands), not production timings. Every number below comes from the artifact
`artifacts/structure/speed-fe887776da0213e1/` (content hash
`fe887776da0213e1…`), served read-only at `GET /api/v1/structure/speed`.

## What was measured

For each of the **14 scenarios** of the Phase-4 benchmark (seeds 2025 and 7 ×
seven families; seed 2025 is the canonical snapshot's network) and each delay in
the Gate-0 grid **0, 1, 6, 12, 24, 48, 72 h**:

- **frozen** — the plan chosen when the shock became known, every action landing
  `d` hours late;
- **replanned** — the existing optimiser (`greedy_objective`, same candidate
  budget: 12 candidates, 2 actions) run again with the earliest landing moved to
  `onset + d`.

Both are replayed against the same shock with common random numbers and compared
with doing nothing. 8 of 14 scenarios have a plan; the other 6 have none at any
delay. Aggregates are means over the 8, with a 95 % percentile bootstrap over
scenarios (2,000 resamples, paired across arms).

| delay | frozen cut % [95 % CI] | replanned cut % | replanned − frozen (pp) |
| --- | --- | --- | --- |
| 0 h | 17.36 [5.40, 30.65] | 17.36 | 0.00 |
| 1 h | 17.21 [5.38, 30.56] | 20.68 [9.43, 32.29] | 3.47 [−0.05, 7.00] |
| 6 h | 17.00 [5.38, 30.24] | 20.34 | 3.34 |
| 12 h | 16.05 [4.99, 28.64] | 19.22 | 3.17 |
| 24 h | 13.79 [4.14, 24.75] | 16.59 | 2.80 |
| 48 h | 7.87 [2.62, 15.73] | 8.27 | 0.40 [0.12, 0.82] |
| 72 h | 1.39 [0.46, 2.77] | 1.38 | −0.01 |

The intervals are wide because eight scenarios is a small sample and three of
them (seed 2025's missed-inflow family) are nearly the same plan.

## Checks

- **δ = 0 reproduces the snapshot exactly** for all seven snapshot scenarios
  (baseline and treated index, cut %, payments kept on time, merchants
  protected, cost: `==`, not approx). The build refuses to write otherwise.
- **Rebuild gives the identical hash**: a slice is rebuilt twice in the test
  suite and must equal the committed records; the full artifact was rebuilt with
  `lce structure verify --rebuild` (result in STATUS).
- **Monotonicity reported, not assumed**: 5 of 8 scenarios fall monotonically in
  both arms. The rises are listed per scenario in the artifact:
  `multi_node_shock` (seed 2025) rises from 1 h to 6 h in both arms; seed 7's
  missed-inflow and partial-payment scenarios rise from 0 h to 1 h in the
  replanned arm (next section).
- Serving imports nothing that builds (subprocess test); no field or label uses
  simulator vocabulary (scan); the artifact is refused if a byte changes.

## Why the replanned arm beats δ = 0 at 1 h

Not because waiting helps. In two seed-7 scenarios the zero-delay plan is two
supplier-term extensions worth **0.02 %**. One hour later the candidate
generator's top-12 list includes injections at m0004 and m0002 that it did not
list at onset; that plan cuts 13.96 % landing at +1 h, and **14.85 % if it had
landed at onset**. So the 1–24 h "replanning gain" is mostly the optimiser
recovering from its own candidate pruning at δ = 0. This is a limitation of the
frozen optimiser, recorded here and not fixed.

## Why at 48 h nothing is kept on time, yet the cut is 20.9 %

Canonical single-missed-inflow scenario, frozen plan:

| delay | cut % | kept on time | share of the cut from: delay term | defaults | deficit-time |
| --- | --- | --- | --- | --- | --- |
| 0 h | 40.36 | ₹12.18 Cr | 97.9 % | 0.5 % | 1.6 % |
| 24 h | 32.93 | ₹2.65 Cr | 98.0 % | 0.6 % | 1.4 % |
| 48 h | 20.93 | ₹0.00 | 97.4 % | 1.0 % | 1.5 % |

"Payments kept on time" counts an obligation only if it is no longer late at all.
By 48 h every payment the money could have saved has already gone late, so the
figure is zero. The disruption index weighs each late payment by *how late* it is
(value × days late), and money arriving at 48 h still shortens those delays —
that is 97 % of the remaining cut. The rest is two defaults avoided and less time
below the liquidity floor. The frontend must show both numbers together, or the
48 h cut reads as payments saved, which it is not.

## Found, not fixed (frozen code)

The snapshot's counterfactual replays the scenario's shock on the **unmutated**
network (`build_scenario_snapshot` uses the build's base graph), while plans are
chosen on the scenario's own network. For the network-mutation families this
leaves the mutation out of the displayed number: `delayed_inflow` shows 40.45 %;
replayed on its own network it is 41.34 %. The value-of-speed artifact follows the
snapshot's replay so that δ = 0 matches what is on screen, and says so in its
provenance (`replay_note`).

## Reproduce

```bash
lce structure build      # ~2 min; refuses to write if δ = 0 ≠ snapshot
lce structure verify --rebuild
lce structure show
pytest tests/test_structure_speed.py
```
