# Status

Resume from this file, `docs/briefs/RUNBOOK.md` and `git log`. Branch `paypal`.
Never push (`origin` push URL is disabled locally).

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

## Leaks

None. `sh scripts/ci/check_commit_trailers.sh` → clean after every commit.

## Needs the human (exact commands, never blocking)

- **Now:** PayPal sandbox keys. At developer.paypal.com, go to Apps & Credentials
  (Sandbox) → Create App (Merchant). Put `PAYPAL_CLIENT_ID`,
  `PAYPAL_CLIENT_SECRET`, `PAYPAL_MODE=sandbox` and
  `LCE_EXECUTION_PROVIDER=paypal_sandbox` in `.env`. Then create two sandbox
  Personal (US) accounts and share their emails.
- **By Nov 9:** the display font. `Against` is personal-use only: swap it for an
  OFL face (one token in `tokens.css`), or license it and keep the file out of
  the public repo.

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
