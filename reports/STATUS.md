# Status

Resume from this file, `docs/briefs/RUNBOOK.md` and `git log`. Branch `paypal`.
Never push (`origin` push URL is disabled locally).

## Phase table

| Phase | Step | Result | Deviations | Needs the human | Next action |
| --- | --- | --- | --- | --- | --- |
| 1 | 1a hygiene + baseline | **PASS** — 517 passed / 8 deselected; ruff, mypy clean; audit 27; phase-4 28; anchors unchanged | Push URL was *not* disabled; disabled it locally. | — | 1b |
| 1 | 1b Gate P2 webhooks + failure re-entry | **PASS** (offline) — 539 passed / 8 deselected; audit 30; phase-4 28; migration 0004 single head, zero diff | Refresh no longer re-POSTs; PayPal events get their own table (see decisions). | Register a sandbox webhook once a public URL exists. | 1c |
| 1 | 1c Gate H value of speed | pending | | | |
| 1 | 1d hosting readiness | pending | | | |
| 1 | 1e concept boards → **STOP** | pending | | design pick | |

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

## Leaks

None. `sh scripts/ci/check_commit_trailers.sh` → clean after every commit.

## Needs the human (exact commands, never blocking)

- Two PayPal sandbox **personal** accounts for the canonical plan's merchants:
  `lce paypal map m0005 --email <account 1>` and
  `lce paypal map m0009 --email <account 2>`.
- After a public URL exists: register `<url>/api/v1/webhooks/paypal` as a sandbox
  webhook for `PAYMENT.PAYOUTS-ITEM.*` and `PAYMENT.PAYOUTSBATCH.*`, and put its id
  in `.env` as `PAYPAL_WEBHOOK_ID`.
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
