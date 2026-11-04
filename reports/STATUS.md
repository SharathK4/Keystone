# Status

Resume from this file, `docs/briefs/QUIET_DEMO.md` (the current brief),
`docs/briefs/RUNBOOK.md` and `git log`. Branch `paypal`. Never push (`origin`
push URL is disabled locally).

## Phase table

| Phase | Step | Result | Deviations | Needs the human | Next action |
| --- | --- | --- | --- | --- | --- |
| 1 | 1a hygiene + baseline | **PASS** — 517 passed / 8 deselected; ruff, mypy clean; audit 27; phase-4 28; anchors unchanged | Push URL was *not* disabled; disabled it locally. | — | 1b |
| 1 | 1b Gate P2 webhooks + failure re-entry | **PASS** (offline) — 539 passed / 8 deselected; audit 30; phase-4 28; migration 0004 single head, zero diff | Refresh no longer re-POSTs; PayPal events get their own table (see decisions). | Register a sandbox webhook once a public URL exists. | 1c |
| 1 | 1c Gate H value of speed | **PASS** — 551 passed / 8 deselected; audit 32; phase-4 28; δ=0 == snapshot exactly; full rebuild hash identical (`fe887776da0213e1`) | Replays follow the snapshot's (unmutated-network) convention; replanned arm beats δ=0 in 2 scenarios (optimiser pruning). See `reports/structure/gate_h.md`. | — | 1d |
| 1 | 1d hosting readiness | **PASS** (no deploy) — 565 passed / 8 deselected; audit 33; phase-4 28 | Render Workflows dropped (no free tier, separate service; jobs take seconds). Readiness check needs the DB (frozen). | Create the Blueprint, enter secrets, register the webhook, record the canonical run. | 1e |
| 1 | 1e concept boards → **STOP** | **DONE** — boards A, B, C + font specimen + index under `design/concepts/` (SVG with subset OFL fonts embedded, PNG via headless Chrome); each rendered and self-critiqued | Ledger hero and chart layout reworked after review (see decisions) | **Pick A, B or C**: open `design/concepts/index.html` | Phase 2 after the pick |
| 2 | Phase 2 frontend | **IN PROGRESS** — new analyses folded into the original four-card design (+ card 05); 576 passed / 8 deselected; audit 35; phase-4 28 | Human chose B, then redirected: keep the original Keystone design and add the new work to it (see decisions) | Review the cards; map sandbox emails; say "run live tests" | State screenshots to docs/screens, axe + keyboard + reduced-motion checks, README screenshots, tag phase-2 |
| 2 | 2b review fixes (Nov 3) | **PASS** — USD display by default (`GET /display`), card 05 as three claims, audience line, ring-label / token / grey / payable-total fixes, `scripts/live_sandbox.sh`; full suite green (5 new tests); ruff, mypy clean; audit 35; phase-4 28; checked in the browser at 1440 and 375 wide | ink-4 still below AA for small text (see decisions) | PayPal sandbox keys + two personal-account emails; font decision by Nov 9 | Live spikes S1–S3 once keys exist, then Render deploy and the failure loop |
| Q | Day 1 (Nov 3): quiet demo, scenes 1–3 + recorded pipeline | **PASS** (offline) — walk backend (`/walk`, `/walk/fire`, `/walk/ask`, `/walk/speed`), delivery capture + `lce walk record`, `lce walk build-speed` artifact `walk-7bdc15a35a48a3f3`, `lce paypal doctor`; front page = scenes 1, 2, 3, 6 with Evidence at `#evidence`; Instrument Serif replaces Against; checks: see the Day 1 report below | Thread is m0038, not the brief's m0013 (see decisions). What-if refuses duration. No recorded real delivery yet, so local fire uses a development event (stamped). | Keys, then `lce paypal doctor`; map m0038 (failing), m0005, m0009; deploy + webhook; "run live tests" | Nov 5: deploy, trigger research, record the real delivery. Nov 6–7: AI layer, scenes 4–5 (full walk plan) |

## Decisions taken

- **1a** `origin`'s push URL still pointed at GitHub, contrary to the runbook. Set it
  to `DISABLED-do-not-push` (local config, reversible:
  `git remote set-url --push origin https://github.com/SharathK4/Keystone.git`).
- **1a** The five unpushed commits were rewritten once (runbook 1.1(d)) to remove
  attribution lines. Backup: lightweight tag `backup/pre-trailer-strip` (old
  `ca06879`). `git diff backup/pre-trailer-strip be676c1` is empty; hashes changed:
  `2f964ac→0accb3a`, `3ee7b81→0839e38`, `c3784a6→76d9265`, `69c260c→bf53a46`,
  `ca06879→be676c1`. `origin/master` (`0a699b0`) untouched.
- **1a** The trailer check reads only annotated tag messages; lightweight tags
  (the backup) have no message of their own.
- **1a** Canonical artifacts committed and stored byte-for-byte via
  `.gitattributes` (`artifacts/** -text`); stored blobs verified equal to the
  working files. Shell scripts and hooks pinned to LF so `sh` can run them on any
  checkout.

- **1b** `refresh` / `lce paypal reconcile` only read. When a create response was
  lost and no batch id is known, the record stays `unknown` with that reason until
  PayPal's batch event arrives; a retry needs a fresh approval.
- **1b** PayPal webhook events use a new table `paypal_webhook_events` (migration
  0004). The existing `webhook_events` has a BIGINT key that SQLite will not
  auto-increment, so inserts fail on SQLite — a latent issue in frozen code, left
  untouched and recorded in `reports/paypal/gate_p2.md`.
- **1b** Re-entry analysis (~1.5–1.7 s in-process) runs after the 200 via
  background tasks, never inside the acknowledgement. One analysis per
  (record, trigger).
- **1b** Simulator-signed events (`webhook_id = "WEBHOOK_ID"`) are accepted only
  with `PAYPAL_ACCEPT_SIMULATOR_EVENTS=true`; default off.
- **1b** Property tests enumerate every event sequence up to length 5 instead of
  adding `hypothesis` as a dependency.

- **1c** The value-of-speed artifact covers the Phase-4 benchmark's 14 scenarios
  (seeds 2025 and 7), regenerated with the current generator; the old
  `reports/phase4/p4-f61b675fa8a867c1` dataset ids predate it and are not reused.
- **1c** "Frozen" delays every action of the plan (both are injections in the
  canonical plan); "replanned" reruns `generate_actions` + `greedy_solve` with
  `decision_time = onset + d`. Systemic importance, baselines, MILP, exact and
  robust stages are skipped: they do not affect the greedy plan.
- **1c** Replay uses the scenario's shock on the unmutated network, exactly as
  the snapshot's counterfactual does, so δ=0 matches what is on screen. Found,
  not fixed (frozen): for `delayed_inflow` the snapshot shows 40.45 %; on its own
  mutated network the same plan cuts 41.34 %.
- **1c** Found, not fixed (frozen optimiser): in two seed-7 scenarios the
  zero-delay plan is worth 0.02 % because the top-12 candidate list at onset
  omits injections it lists an hour later (those cut 14.85 % at onset). Reported
  as monotonicity rises, not smoothed.
- **1c** 48 h explanation: the remaining 20.9 % cut is 97 % delay term (payments
  still late, but less late), 1 % defaults avoided, 1.5 % deficit-time. Payments
  kept on time is ₹0. The UI must show both together.

- **1d** Render Workflows not adopted (`reports/hosting/render.md`): Python SDK
  exists, but it is a separate paid service with no stated free tier and our jobs
  take seconds; the in-process background tasks stay.
- **1d** Demo safety is a FastAPI dependency on approve / execute / refresh only;
  `LCE_DEMO_MODE=off` (default, and pinned in the test session) changes nothing.
  The rate limit is in memory per process — a courtesy limit for token holders.
- **1d** `lce demo reset` is a subcommand on the existing `demo` parser; `lce demo
  -n …` parses exactly as before (tested).
- **1d** Frontend: `VITE_API_BASE` (default empty = same origin) as the fallback
  if the static-site `/api/*` rewrite does not proxy POSTs.
- **1d** `/api/v1/health/ready` requires the database (frozen). The blueprint uses
  it as the runbook says; when the free database expires the check fails while
  the analysis still serves. Not changed.
- **1d** README is pre-PayPal: "Nothing moves money" and the old provider's name
  are stale. Left for the Phase 3 README rewrite; only the Deploy section added.

- **1e** Fonts measured: only IBM Plex Mono has ₹ (U+20B9) among mono candidates
  (JetBrains Mono, DM Mono, Courier Prime lack it), so all boards use it for
  figures. Display serifs: Source Serif 4 (A), Libre Caslon Text (B), Spectral (C).
  Packages pinned in `design/package.json`; licences still to copy into the repo
  and list in `docs/ASSETS.md`.
- **1e** The three personal-use-only demo font zips in `fonts/` are untracked on
  this branch (kept on disk, ignored). `frontend/public/fonts/against-regular.otf`
  (same licence) is still served by the current frontend until Phase 2 replaces it.
- **1e** Narrative risk: no snapshot scenario has a multi-hop cascade (max depth
  1; only shocked merchants affected). The canonical 40.4 % cut is on the whole
  week's network disruption (14 merchants miss payments, 13 would anyway), and
  the plan shores up two keystones already short of cover (m0005, m0009). Copy
  must say this plainly.

- **1e** Self-critique fixes applied before presenting: A's staggered courses
  trimmed flush, keystone marks moved to the ground line, shoring drawn as raking
  shores in front of the wall, the cost chart re-laid out; B's keystone labels
  collapsed into one bracket note; C laid out on connected merchants only (the 15
  with no inferred dependency are drawn apart as labelled islands), title moved
  out of the chart so it hides no data.
- **1e** Board generator `design/build_concepts.py` (Python, reads the committed
  artifacts) and renderer `design/render.mjs` (playwright-core driving local
  Chrome; downloads no browser). Rebuild: `python design/build_concepts.py &&
  node design/render.mjs` after `npm ci` in `design/`.
- **1e** Open for Phase 2 (`docs/ASSETS.md`): subsets of Reserved-Font-Name faces
  (Source Serif 4; IBM Plex likely) — serve unmodified files or confirm the OFL
  FAQ position before shipping.

- **Phase 2** The human picked Board B, then redirected: the original Keystone
  frontend (four textured cards, analysis over the page) stays, and the new work
  goes into it. The full Ledger Room rebuild was discarded uncommitted. Added:
  01 inferred-strength recovery; 02 pull any merchant (offline pulls artifact +
  compute now); 03 the cost of waiting; 04 provider-neutral contract card whose
  review opens approval → PayPal sandbox payout → ledger → re-entry; 05 the
  proof card and certificate. Provenance seals on every new figure, derived
  from the endpoint.
- **Phase 2** Fonts: the human chose to keep the original display face
  (`Against`, a personal-use demo licence) for the demo; recorded here and in
  `docs/ASSETS.md`. Swapping it is one token in `tokens.css`.
- **Phase 2** Razorpay branding removed from the UI and the frontend source
  (card renamed `ContractCard`, `--rzp` tokens renamed); "simulator" removed from
  user-facing copy in the views touched.
- **Phase 2** New artifacts: `pulls-36882cbbf5e020ed` (100 merchants × 1.5/2/2.5×
  slack, ~12 min to build) and `proof-c264028509d38f71`. Proof finding: links
  are observed, not discovered (216/216); strengths are inferred with rank
  correlation 0.85 (pass-through), 0.64 (lag), 0.38 (conditional probability).
  11 of 100 pulls reach one hop; none go further.
- **Phase 2** `scripts/dev_api.sh` runs the API for frontend work with PayPal
  deliberately unconfigured, so no page can call PayPal.

- **Phase 2b (Nov 3)** External review of the submission. The four findings
  acted on: PayPal judges read crore/lakh poorly, card 05 led with
  housekeeping checks, a few UI defects, and the page never named its audience.
  These are not yet done: a live sandbox run, a visible AI layer, invoicing,
  the deploy, the font licence, the public repo, the README, the video and the
  Devpost text. Plan to Nov 12: live PayPal → Render deploy + failure loop →
  bounded AI layer + invoicing → stress network (one day at most) → repo,
  README, Devpost → video → submit Nov 11.
- **Phase 2b** Money is shown in **US dollars by default** at a declared round
  rate, 0.01 USD per analytic rupee (`LCE_DISPLAY_*`, served by the additive
  `GET /api/v1/display`), labelled as not a market rate. The rate is exactly
  1000× the sandbox scale, so "$773K analytic → $772.69 paid" reads at a
  glance. `PAYPAL_DEMO_SCALE` is unchanged. The wire stays in rupees, and a
  masthead switch restores them. If `/display` is missing the page stays in
  rupees.
- **Phase 2b** Card 05 makes three claims (PayPal fenced; plans scored by
  replay; dependencies estimated before the shock). Each counts its backing
  audit lines from the proof artifact, so a failed or missing check shows.
- **Phase 2b** Grey ramp darkened for legibility: ink-3 2.8:1 → 4.8:1 and
  ink-4 1.9:1 → 3.4:1 on surface. ink-4 is still below 4.5:1 for small text;
  full AA there would flatten the hierarchy.
- **Phase 2b** `scripts/live_sandbox.sh` serves the API, or runs any `lce`
  command, with the sandbox keys read from `.env` and its own
  `artifacts/runs/live.sqlite`. Live mode stays refused in code.

- **Q1 (Nov 3)** The quiet-demo brief replaces the final stretch. It is saved
  verbatim as `docs/briefs/QUIET_DEMO.md`, and `CLAUDE.md` points at it.
- **Q1** **Thread = m0038 at 2x its cushion**, not the brief's example m0013.
  Measured on the same machinery:
  - m0013 reaches two more merchants (39 h, one step). But its plan includes
    a supplier term extension PayPal cannot pay. And its re-planned arm jumps
    10.3% → 25.4% at 1 h: the search at the moment of the shock misses an
    m0009 injection it lists an hour later (the Gate H candidate-list
    weakness, frozen optimiser).
  - m0038 reaches one more merchant (m0059, about 4 days, one step out). Its
    plan is two injections PayPal can pay (m0005, m0009). Both curves fall
    monotonically: payments kept on time are $1.62M until 12 h, $1.28M at
    24 h, $29.8K at 48 h and $0 at 72 h; the cut goes 41.8% → 18.4%.
  - Scene 2 therefore says "One more feels it." Override with
    `LCE_WALK_MERCHANT` and `LCE_WALK_MULTIPLE`.
- **Q1** The failing payment is modelled as what the pipeline already handles:
  a PayPal payout to m0038 that fails ("walk trigger"). It has its own plan
  hash, an approval row naming who fired it, and one execution record keyed
  by an idempotency key. Its failure re-enters through the existing
  failed-payout re-entry, so its shock is exactly "m0038 loses 2x its cushion",
  the pull the offline artifact replayed.
- **Q1** **Recorded pipeline.** With `LCE_RECORD_DELIVERIES_DIR` set
  (`live_sandbox.sh` sets it), the webhook route keeps each verified delivery
  byte for byte: raw body plus the five signature headers, never the webhook
  id. `lce walk record <event id>` then writes
  `artifacts/paypal/recorded/walk.json` with PayPal's certificate (downloaded
  from the URL the delivery names) and the trigger record. It refuses a
  capture that does not verify.
  - Replay (`POST /walk/fire`) restores the trigger in a fresh database and
    verifies the bytes against that certificate and `PAYPAL_WEBHOOK_ID`, then
    ingests and analyses them. There is no network call in recorded mode.
  - Until a real delivery is recorded, `LCE_ENV` local/test fires a
    development event signed in process. It is stamped "development event,
    signed locally, not from PayPal" and refused when the hosted demo mode is
    on.
  - The first fire persists the re-entry analysis exactly as a webhook would;
    later fires recompute it for the page.
- **Q1** What-if parsing has a `ShockParser` interface with a no-key
  `RuleParser`. The language-model parser arrives on Nov 6 under the same
  schema and refusals.
  - It parses who is hit, size (0.5x–4x the cushion) and onset (within the
    week).
  - It refuses **duration**: the analysis's shortfall lasts the rest of the
    week. So the brief's placeholder "what if it lasts three days?" became
    "what if it were twice as large?", and the duration question is refused
    with its reason.
- **Q1** **Scene 3 data.** The walk's cost of waiting is an offline artifact
  built on the bounded analysis's own machinery (the snapshot's estimated
  network, the same candidates, greedy search and replay). At zero delay it
  equals the live analysis (tested), whereas Gate H's generator-based curve
  would have been someone else's shock. Payments kept on time is the plotted
  measure.
- **Q1 (found)** The on-demand analysis response names only the **first**
  selected action. `alternatives` excludes selected options, so the walk plan's
  second injection (m0009) appears nowhere in it. The replay artifact lists
  both. Scenes 4–5 (Nov 6) must take the full plan from there, or add an
  additive field. `build_plan` on the live response would be incomplete.
- **Q1** The front page is the walk; the gallery is "Evidence" at
  `#evidence` and its old panel hashes. Closing a panel now keeps
  `#evidence` (the only change to the gallery). Scenes 4 and 5 are not built
  yet, and their progress ticks are dimmed.
- **Q1** Instrument Serif (OFL, `@fontsource` 5.3.0) is the one `--display`
  face, and `ASSETS.md` and the licence text are updated. `against-regular.otf`
  is still in the tree, unloaded, until the publish copy.
- **Q1** `lce paypal doctor` reads only. PayPal is called only with `--probe`
  and the deployment only with `--public-url`. It reads plan payees from the
  replay artifact.

## Day reports (quiet demo)

**Day 1, Nov 3.**
- **Passed:**
  - 617 tests passed (8 live-only deselected); ruff and mypy clean; audit 35; phase-4 28; anchors untouched.
  - The walk fires end to end through the real verifier, state machine and re-entry: m0038 missed $397K → m0059 runs short four days later, one step out. Computed live in about 5 s.
  - The what-if parses, recomputes in about 3.6 s and refuses with reasons.
  - Scene 3's zero-delay point equals the live analysis.
  - Content covers 7–17% of a 1440×900 scene and at most 34.5% of a 390×844 one; `getAnimations()` is empty after load and after a fire.
- **Failed:** none.
- **Unverified:**
  - Everything involving real PayPal: no keys yet, no recorded delivery.
  - The trigger behaviour (an unclaimed address is the candidate).
  - Playwright, axe and the keyboard walk are due Nov 8.
- **Needs the human:** keys in `.env`; `paypal doctor`; map m0038, m0005, m0009; deploy and webhook.
- **Next:** Nov 5 deploy, trigger research and recording. Nov 6–7 the LLM parser and narration, and scenes 4–5 using the full plan from the replay.

**Nov 4 check.**
- **Anthropic:** the key is in `.env` and verified with one read-only model listing (200).
- **PayPal:** the keys were **not** in `.env` (it was last saved on Sep 2). Empty `PAYPAL_*` lines were added for the owner to fill.
- **Identities** are mapped in `artifacts/runs/live.sqlite`:
  - m0005 → `sb-47gmny53708508@personal.example.com` (US personal);
  - m0009 → `sb-x0xaw53687003@personal.example.com` (**IN** personal);
  - m0038 → `keystone-m0038-unclaimed@example.com` (no account, so its payout should go unclaimed).
- **The live config serves:**
  - The canonical plan is complete and payable to mapped accounts ($1,123.40).
  - The walk fires through re-entry, which is now *analysed* and persisted because m0038 is mapped.
- **Tests:** 617 passed with the new `.env`.
- **Risk:** the sandbox payer account is **IN** (the developer account's default). Whether an IN business account may send Payouts is unverified. The payouts probe settles it once keys exist; the fallback is a US Business sandbox account with an app created under it.

**Nov 4 probe.**
- **Keys:** the owner pasted the PayPal keys. Saving the file from their editor dropped the Anthropic key I had added, so it was re-added.
- **Read-only probes:**
  - token issued;
  - Payouts permitted (missing batch → 404);
  - Invoicing **403 NOT_AUTHORIZED** (invoicing is cut anyway);
  - webhooks API 200.
- **The app:** after a restart, `/execution/status` reports configured with Payouts available, and the canonical plan is payable to mapped accounts ($1,123.40). The desk reads "PayPal sandbox connected".
- **Still unverified:** a real payout from the **IN** payer. A probe cannot tell whether it holds USD. If its balance is INR, USD payouts may fail, since India is excluded from payouts currency conversion. S2 (one $1 payout) settles it; the fallback is a US Business sandbox app.

## Leaks

None. `sh scripts/ci/check_commit_trailers.sh` → clean after every commit.

## Needs the human (exact commands, never blocking)

- **Now:** PayPal sandbox keys. At developer.paypal.com, go to Apps & Credentials
  (Sandbox) → Create App (Merchant). Put `PAYPAL_CLIENT_ID`,
  `PAYPAL_CLIENT_SECRET`, `PAYPAL_MODE=sandbox` and
  `LCE_EXECUTION_PROVIDER=paypal_sandbox` in `.env`. Then run
  `sh scripts/live_sandbox.sh paypal doctor`; it lists everything still missing.
- **Identities for the walk** (doctor prints the same):
  - **m0005, m0009**: two sandbox Personal (US) accounts, the plan's payees:
    `sh scripts/live_sandbox.sh paypal map m0005 --email <account 1>` and the
    same for m0009.
  - **m0038**: the failing payout. Map it to an address with **no** PayPal
    account, so the payout should go unclaimed (to be confirmed in trigger
    research): `sh scripts/live_sandbox.sh paypal map m0038 --email <address>`.
- **Nov 5:** deploy (README → Deploy), register the webhook, say "run live
  tests". Trigger research then settles which payout fails fast (an unclaimed
  address is the first candidate). The live trigger route is built on that
  answer, fired once, and recorded:
  `sh scripts/live_sandbox.sh walk record <event id>`.
- ~~By Nov 9: the display font~~ — settled by the quiet-demo brief: Instrument
  Serif is live; Against is removed from the publish copy.

- The public original (`origin/master`) already contains personal-use-only demo
  fonts (`fonts/*.zip`, `frontend/public/fonts/against-regular.otf`). Nothing is
  pushed from here; decide whether to remove them there.

- Two PayPal sandbox **personal** accounts for the canonical plan's merchants,
  mapped against the live database:
  `sh scripts/live_sandbox.sh paypal map m0005 --email <account 1>` and
  `sh scripts/live_sandbox.sh paypal map m0009 --email <account 2>`.
- After a public URL exists: register `<url>/api/v1/webhooks/paypal` as a sandbox
  webhook for `PAYMENT.PAYOUTS-ITEM.*` and `PAYMENT.PAYOUTSBATCH.*`, and put its id
  in `.env` as `PAYPAL_WEBHOOK_ID`.
- Render (when ready): create a Blueprint from `render.yaml`, enter the
  `sync: false` values (sandbox client id/secret, webhook id, a private
  `LCE_DEMO_TOKEN`), check the API address against the `/api/*` rewrite, then
  record the canonical run with the token header. Steps in README → Deploy.
- Say **"run live tests"** in chat to run `pytest -m paypal_live` (spikes S1–S3).

## Checks to run at any checkpoint

```bash
python -m ruff check src tests
python -m mypy src
python -m pytest -p no:cacheprovider
PYTHONPATH=src python src/lce/scripts/audit_backend.py
PYTHONPATH=src python src/lce/scripts/verify_phase4.py && git checkout -- reports/phase4/
sh scripts/ci/check_commit_trailers.sh
```
