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
| 1 | 1e concept boards → **STOP** | **IN PROGRESS** — Board A generator + headless renderer written (`design/build_concepts.py`, `design/render.mjs`); A renders (1440x2280) but is not yet reviewed | — | design pick (after B, C, specimen) | Resume: review A's PNG, write `board_b` (Ledger Room), `board_c` (Survey Chart), specimen, `index.html`, a `main()`; render; critique; un-ignore `design/concepts/`; commit; tag `phase-1`; STOP |

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

## Leaks

None. `sh scripts/ci/check_commit_trailers.sh` → clean after every commit.

## Needs the human (exact commands, never blocking)

- The public original (`origin/master`) already contains personal-use-only demo
  fonts (`fonts/*.zip`, `frontend/public/fonts/against-regular.otf`). Nothing is
  pushed from here; decide whether to remove them there.

- Two PayPal sandbox **personal** accounts for the canonical plan's merchants:
  `lce paypal map m0005 --email <account 1>` and
  `lce paypal map m0009 --email <account 2>`.
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
